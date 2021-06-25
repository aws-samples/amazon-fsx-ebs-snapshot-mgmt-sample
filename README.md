## FSx Backup and EBS Snapshot Management
### Table of Contents
* [Overview](#Overview)
* [Prerequisites](#Prerequisites)
* [Workflow](#Workflow)
* [Deployment](#Deployment)
* [UsageNotes](#UsageNotes)
* [Troubleshooting](#Troubleshooting)
* [Security](#Security)
* [License](#License)
--------------------------------------------------------------------
### Overview
This sample code will illustrate the use of several AWS services to build a simple data backup solution based upon FSx Windows FileSystem backups and EC2 EBS volume snapshots. It is geared toward Windows based environments, however the concepts could be adapted for other use-cases. The intent is as a data backup solution and not as a host level operating system backup solution. The solution is a combination of Cloudformation, Lambda, Step Function, Python, and Windows Powershell code.

This solution can be used as a backup strategy for non-production environments in order to keep costs down by alleviating the need to use your production level backup solution. This might also be a good fit for retaining multiple point in time backups which can be used to build or refresh environments for testing purposes, being able to roll forward or backward as needed by simply restoring the appropriate FSx backup or creating EBS volumes from the snapshots.

The sample code will also show how you might perform any required Pre or Post backup operations such as stopping a service or application prior to taking a snapshot and restarting them after the snapshot is complete.

### Prerequisites
The following items are required to deploy this solution for testing and evaluation purposes.
* Administrative permissions to your AWS account, or at least enough permissions to deploy Cloudformation templates, create IAM roles, Lambda functions, Step Functions, SSM Automation Documents and Eventbridge rules.
* An S3 bucket to upload the lambda code zip files to.
* One or more EC2 instances with additional EBS volumes attached. The EC2 instances need to have the tag below. This tag is used to locate the EC2 instances that should have snapshots created of their EBS volumes
  - Tag Key: 'ResourceType'
  - Tag Value: 'SnapshotMgmtTarget'
* EC2 instance profile attached to your EC2 instances with the required permission to allow management via Aws Systems Manager and the ability to execute SSM commands and documents, create EBS snapshots and tags. A sample [Cloudformation template](cloudformation/iam/ec2-profile-and-role.yaml) that will create an IAM role and EC2 instance profile with the required permissions can be found at:
  - 'cloudformation/iam/ec2-profile-and-role.yaml'
* One or more Windows FSx file systems. The file systems need to have the tag below. This tag is used to locate the file systems that should have backups created by this solution.
  - Tag Key: 'ResourceType'
  - Tag Value: 'SnapshotMgmtTarget'
### Workflow
1. The Amazon EventBridge scheduled rule will invoke the SnapshotMgmt-SFNTrigger lambda function. The lambda function can also be invoked manually for testing via the GUI or through an API or CLI call. The event data passed to the function is not important.
2. The SnapshotMgmt-SFNTrigger lambda will perform the following actions:
   1. Enumerate all FSx file systems, selecting those with a specific tag
   2. Enumerate EC2 instances, specifying a filter to select those with a specific tag
   3. Start the execution of the SnapshotMgmtSample step function, passing in the FSx file system and EC2 instance information as input
3. The SnapshotMgmtSample step function will execute two branches in parallel. One branch will perform all of the FSx file system operations and one branch will perform all of the EBS snapshot operations. Both branches will invoke the SnapshotMgmt lambda function to perform the various operations within the workflow.
   * The 'ProcessFSxFileSystems' branch will perform the following actions:
     1. In the 'CheckBackupInProgress' step, check if an FSx backup is already in progress for the specified file system. Only a single FSx backup can be taken at a time for a given file system.
     2. If a backup is already in progress, 'Wait' for 60 seconds and check again to see if a backup is still in progress. This wait/loop state can continue for up to 30 minutes at which time the 'BackupTimeout' step will be entered and the step function will fail.
     3. If no backup is in progress, the 'BackupFSxFileSystem' step will invoke the SnapshotMgmt lambda to create an FSx backup of the specified file system.
     4. The 'CleanupFSxBackups' step will search for FSx backups for the specified file system, locating backups that are older than the defined retention period, and deleting them.
   * The 'ProcessEc2Instances' branch will perform the following actions:
     1. The 'SnapshotEbsVolumes' step will execute an SSM automation document which will perform any pre/post snapshot steps as well as freeze the I/O to the EBS volumes and create a snapshot of each.
     2. The 'GetExecutionStatus' step will retrieve the execution status of the SSM automation document.
     3. Based upon the automation execution status the 'ExecutionStatus' step will
        1. Enter the 'InProgress' step and wait for 40 seconds before checking the execution status again.
        2. If the execution status returns a failed state, indicating that the EBS snapshot creation has failed, and this has occurred less than 5 times the 'RetryEbsSnapshot' step will execute the SSM automation document again to retry the snapshot process.
        3. If the execution status has failed 5 times the 'Failed' state will be entered and the step function will fail
        4. Upon successful EBS snapshot creation, the 'Success' step will pass the EC2 instance IDs to the 'CleanupOldSnapshots' step
     4. The 'CleanupOldSnapshots' step will search for EBS volume snapshots tagged with the EC2 instance ID, determining if the snapshots are older than the defined retention period and deleting them.

<p align="center">
  <img src="resources/workflow.png" width="750" title="hover text">
</p>

## Deployment
1. Zip the Lambda function code.
   1. Zip the file 'lambda/sfn-trigger/lambda_function.py' into a zip file named 'sfn-trigger.zip'
   2. Zip the file 'lambda/snap-manager/lambda_function.py' into a zip file named 'snap-manager.zip'
2. Upload the Lambda zip files to an S3 bucket
3. Deploy the Snapshot Management Cloudformation template.
   1. Open the Cloudformation management console and select the Cloudformation template:
      1. 'cloudformation/snapshot-management.yaml'
   2. Provide the values for the following Cloudformation parameters:
      1. **Stack name:** The Cloudformation Stack name
      2. **S3Bucket:** An S3 bucket name. This should be the S3 bucket where you have uploaded the Lambda zip files.
      3. **SFNTriggerLambdaZipKey:** The S3 object path (key) for the 'sfn-trigger.zip' file
      4. **SnapshotManagementLambdaZipKey:** The S3 object path (key) for the 'snap-manager.zip' file
      5. **LambdaKmsKeyArn:** The ARN of the Kms Key that will be used to encrypt the Lambda function environment variables. You may use your own Customer managed key or the AWS managed key for Lambda
      6. **TimeDurationType:** A time duration type value which is used to determine the retention period for the EBS snapshots and FSx backups. Valid values are: (days, seconds, microseconds, milliseconds, minutes, hours, weeks)
      7. **TimeDuration:** A numeric value indicating the lenth of the retention period for the EBS snapshots and FSx backups. For example, how many (days, hours, minutes, etc.) to retain the snapshots and backups for.

## Usage Notes
* The Amazon EventBridge rule which invokes the SnapshotMgmt-SFNTrigger lambda function based upon a schedule is deployed as part of the solution in a 'DISABLED' state. You may either manually 'ENABLE' it after deployment, or modify the 'State' property of the 'SFNTriggerLambdaEventRule' resource in the 'snapshot-management.yaml' Cloudformation template.
* If you would like to change the tag key and value that are used to locate the FSx file systems and EC2 instances, those are specified as 'Environment' > 'Variables' properties on the 'SFNTriggerLambda' resource in the 'snapshot-management.yaml' Cloudformation template.
* If you need to perform any Pre or Post steps prior to creating the EBS volume snapshots, there are placeholder steps in the SSM automation document named 'PreSnapshotExecution' and 'PostSnapshotExecution'. You may insert your custom code in these SSM document steps. The Cloudformation resource name for the SSM automation document is 'EBSVolumeSnapshotDocument' in the 'snapshot-managmement.yaml' Cloudformation template.
* By default the operating system EBS volume is excluded from the EBS snapshot process. If you wish to exclude addtional volumes you may modify the code in the SSM automation document 'Vss-Snapshot' function with conditional logic to include additional 'DeviceName' values.
* An attempt is made to create the EBS volume snapshots as 'application-consistent' snapshots, if this is successful the snapshots will have a tag of 'AppConsistent=True'. You can read more about this process in the EC2 documentation.
  - https://docs.aws.amazon.com/AWSEC2/latest/WindowsGuide/application-consistent-snapshots.html

## Troubleshooting
* When attempting to create an EBS volume snapshot using this solution, the volume must be online and initialized within the host operating systems or the snapshot process will fail
* For additional troubleshooting steps you can refer to the documentation below.
  - https://docs.aws.amazon.com/AWSEC2/latest/WindowsGuide/application-consistent-snapshots-creating-commands.html#application-consistent-snapshots-troubleshooting
## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
