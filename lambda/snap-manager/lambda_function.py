#! /usr/bin/env python3
"""
This lambda invokes the SSM documents to perform the EC2 instance snapshots
as well as creating FSx backups and performing snapshot and backup cleanups.
"""

import sys
import os
import json
import logging
import boto3
import time
from operator import itemgetter
from datetime import datetime, timedelta, timezone
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def jsonify(data):
    """Make data json serializable."""
    if isinstance(data, dict):
        for key in data.keys():
            # if a datetime object is found, convert it to a string
            if 'datetime.datetime' in str(type(data[key])):
                data[key] = data[key].strftime('%Y-%m-%dT%H:%M:%S')
            elif isinstance(data[key], list):
                jsonify(data[key])
            elif isinstance(data[key], dict):
                jsonify(data[key])
    elif isinstance(data, list):
        for item in data:
            if 'datetime.datetime' in str(type(item)):
                # if a datetime object is found, convert it to a string
                item = item.strftime('%Y-%m-%dT%H:%M:%S')
            elif isinstance(item, dict):
                jsonify(item)
            elif isinstance(item, dict):
                jsonify(item)
    return data


def jprint(data):
    """Pretty print json."""
    print(json.dumps(jsonify(data), indent=4))


def get_execution_status(ev_obj):
    """Get an automation execution status."""
    ssm = boto3.client('ssm')
    ex_data = ev_obj['ExecutionData']
    status = 'InProgress'

    eid = ex_data['ExecutionId']
    resp = ssm.describe_automation_executions(
        Filters=[{'Key': 'ExecutionId',
                  'Values': [eid]}])
    ae_stat = resp['AutomationExecutionMetadataList'][0]
    stat = ae_stat['AutomationExecutionStatus']
    logger.info('AutomationExecution: %s status: %s', eid, stat)
    if ('TimedOut' in stat or 'Failed' in stat or 'Cancelled' in stat):
        status = 'Failed'
    elif stat == 'Success':
        status = 'Success'

    if status == 'Success':
        ex_data['Status'] = 'Success'
    elif status == 'Failed':
        ex_data['Status'] = 'Failed'
    else:
        ex_data['Status'] = 'InProgress'
    return ev_obj


def ebs_snap_manager(ev_obj, snap_req_id):
    """Manage Snapshot Creation."""
    # Resolve the SSM parameter to get the SSM document name
    ssm = boto3.client('ssm')
    snap_doc_param = ssm.get_parameter(Name=ev_obj['EbsSnapDocumentParameter'])
    snap_doc = snap_doc_param['Parameter']['Value']

    # Start the SSM document to create the EBS volume snapshots
    exe_ids = {'InstanceId': '', 'ExecutionId': '', 'Status': 'Unknown',
               'ClusterSnapshotEvent': {}}
    if 'AttemptCount' in ev_obj:
        ev_obj['AttemptCount'] += 1
        logger.info('Incrementing AttemptCount to %s', ev_obj['AttemptCount'])
    else:
        ev_obj['AttemptCount'] = 1
        logger.info('Setting initial AttemptCount to 1')

    inst_id = ev_obj['InstanceId']
    # if the instance has a name tag, use that for the snapshot name
    # otherwise use the instance id
    snap_name = inst_id
    for tag in ev_obj['Tags']:
        if tag['Key'] == 'Name':
            snap_name = tag['Value']
    logger.info('Starting automation execution for instance: %s',
                inst_id)
    # start the automation document
    resp = ssm.start_automation_execution(
        DocumentName=snap_doc,
        Parameters={
            'InstanceId': [inst_id],
            'SnapshotName': [snap_name],
            'SnapshotRequestId': [snap_req_id]
        })

    logger.info('Execution Id: %s', resp['AutomationExecutionId'])
    exe_ids['InstanceId'] = inst_id
    exe_ids['ExecutionId'] = resp['AutomationExecutionId']
    exe_ids['EBSSnapshotEvent'] = ev_obj
    return exe_ids


def fsx_snap_manager(ev_obj, backup_req_id):
    """Create FSx Backups."""
    fsx = boto3.client('fsx')
    # Reuse any existing tags from the filesystem after they are sanitized
    existing_tags = ev_obj['Tags']
    existing_tags.append(
        {'Key': 'CreatedBy', 'Value': 'Snapshot-Automation-Sample'})
    existing_tags.append(
        {'Key': 'BackupRequestId', 'Value': backup_req_id})
    existing_tags.append(
        {'Key': 'FileSystemID', 'Value': ev_obj['FileSystemId']})
    existing_tags = \
        [tag for tag in existing_tags if not tag['Key'].startswith('aws:')]
    # Create the backup
    resp = fsx.create_backup(
        FileSystemId=ev_obj['FileSystemId'],
        Tags=existing_tags,
        ClientRequestToken=backup_req_id)
    resp['Backup']['CreationTime'] = \
        resp['Backup']['CreationTime'].strftime('%Y-%m-%dT%H:%M:%S')
    logger.info('CREATEBACKUPRESPONSE: %s', resp)
    # Add additional items to the event object that are needed downstream
    for tag in ev_obj['Tags']:
        if tag['Key'] == 'Name':
            ev_obj['FileSystemName'] = tag['Value']
    ev_obj['CreateBackupResponse'] = resp['Backup']
    return ev_obj


def check_backup_status(ev_obj):
    """Check if any backups are running."""
    fsx = boto3.client('fsx')
    tmp_backups = []
    # Get all of the backups
    resp = fsx.describe_backups(Filters=[{'Name': 'file-system-id',
                                          'Values': [ev_obj['FileSystemId']]}])
    tmp_backups.extend(resp['Backups'])
    while 'NextToken' in resp:
        resp = fsx.describe_backups(
                Filters=[{'Name': 'file-system-id',
                          'Values': [ev_obj['FileSystemId']]}],
                NextToken=resp['NextToken'])
        tmp_backups.extend(resp['Backups'])
    # Check if any of the backups are CREATING
    for backup in tmp_backups:
        if backup['Lifecycle'] == 'CREATING':
            ev_obj['BackupInProgress'] = True
            ev_obj['AttemptCount'] += 1
            return ev_obj
    ev_obj['BackupInProgress'] = False
    return ev_obj


def get_ebs_snapshots(ev_obj):
    """Get EBS snapshots."""
    ec2 = boto3.client('ec2')
    ebs_snaps = []
    resp = ec2.describe_snapshots(
            Filters=[{'Name': 'tag:Ec2InstanceId',
                      'Values': [ev_obj['InstanceId']]},
                     {'Name': 'tag:CreatedBy',
                      'Values': ['Snapshot-Automation-Sample']}])
    ebs_snaps.extend(resp['Snapshots'])
    while 'NextToken' in resp:
        resp = ec2.describe_snapshots(
            Filters=[{'Name': 'tag:Ec2InstanceId',
                      'Values': [ev_obj['InstanceId']]},
                     {'Name': 'tag:CreatedBy',
                      'Values': ['Snapshot-Automation-Sample']}],
            NextToken=resp['NextToken'])
        ebs_snaps.extend(resp['Snapshots'])
    return ebs_snaps


def get_duration_object(time_type, duration):
    """Create the correct time duration type for the timedelta object."""
    duration = int(duration)
    switcher = {
        'days': timedelta(days=duration),
        'seconds': timedelta(seconds=duration),
        'microseconds': timedelta(microseconds=duration),
        'milliseconds': timedelta(milliseconds=duration),
        'minutes': timedelta(minutes=duration),
        'hours': timedelta(hours=duration),
        'weeks': timedelta(weeks=duration)
    }
    return switcher.get(time_type, None)


def ebs_snap_cleanup(ev_obj):
    """Remove old snapshots."""
    # Get snapshots with the appropriate tags
    snaps = get_ebs_snapshots(ev_obj)
    logger.info('All Snapshots: %s', snaps)

    # Parse the snaps based on time
    ec2 = boto3.client('ec2')
    old_snaps = []
    delta = get_duration_object(
        os.environ['TimeDurationType'], os.environ['TimeDuration'])
    min_snap_time = datetime.now(timezone.utc) - delta
    for snap in snaps:
        if snap['StartTime'] < min_snap_time:
            old_snaps.append(snap)
    # Sort the old snaps list by date with newest at the top
    old_snaps = sorted(old_snaps, key=itemgetter('StartTime'),
                       reverse=True)
    logger.info('Purging snapshots older than: %s',
                min_snap_time.strftime('%m-%d-%Y_%H:%M:%S'))
    # Purge snapshots older than the defined time
    logger.info('Old Snaps: %s', old_snaps)
    for snap in old_snaps:
        logger.info('DELETE SNAP: %s', snap)
        ec2.delete_snapshot(SnapshotId=snap['SnapshotId'])
        time.sleep(1)


def get_fsx_backups():
    """Get FSx Backups."""
    fsx = boto3.client('fsx')
    fsx_backups = []
    resp = fsx.describe_backups(
            Filters=[{'Name': 'backup-type',
                      'Values': ['USER_INITIATED']}])
    fsx_backups.extend(resp['Backups'])
    while 'NextToken' in resp:
        resp = fsx.describe_backups(
                Filters=[{'Name': 'backup-type',
                          'Values': ['USER_INITIATED']}],
                NextToken=resp['NextToken'])
        fsx_backups.extend(resp['Backups'])
    return fsx_backups


def parse_fsx_backups(backups, tag_key, tag_val):
    """Parse backups by tag."""
    tmp_backups = []
    for bkup in backups:
        for tag in bkup['Tags']:
            if ((tag['Key'] == tag_key) and (tag['Value'] == tag_val)):
                tmp_backups.append(bkup)
                break
    return tmp_backups


def fsx_backup_cleanup(ev_obj):
    """Remove old FSx backups."""
    # Get all of the FSx backups for a file system
    backups = get_fsx_backups()
    logger.info('Number of FSx Backups found: %s', len(backups))
    # Get the backups with a specific tag
    tagged_backups = parse_fsx_backups(backups, 'FileSystemID',
                                       ev_obj['FileSystemId'])
    tagged_backups = parse_fsx_backups(tagged_backups, 'CreatedBy',
                                       'Snapshot-Automation-Sample')
    tagged_backups_cnt = len(tagged_backups)
    logger.info('Number of tagged FSx Backups found: %s',
                tagged_backups_cnt)
    logger.info('Tagged Backups: %s', tagged_backups)
    # Parse the backups based on time
    fsx = boto3.client('fsx')
    old_backups = []
    delta = get_duration_object(
        os.environ['TimeDurationType'], os.environ['TimeDuration'])
    min_backup_time = datetime.now(timezone.utc) - delta
    for bkup in tagged_backups:
        ctime = bkup['CreationTime']
        if ctime < min_backup_time:
            old_backups.append(bkup)
    # Sort the old backup list by date with the newest at the top
    old_backups = sorted(old_backups, key=itemgetter('CreationTime'),
                         reverse=True)

    logger.info('Purging backups older than: %s',
                min_backup_time.strftime('%m-%d-%Y_%H:%M:%S'))

    # Purge backups older than the defined time
    logger.info('Old Backups: %s', old_backups)
    for bkup in old_backups:
        logger.info('DELETE BACKUP: %s', bkup)
        fsx.delete_backup(BackupId=bkup['BackupId'])
        time.sleep(1)


def lambda_handler(event, context):
    """Invoke the lambda function."""
    logger.info('EVENT: %s', event)
    if event['EventType'] == 'EBSSnapshot':
        event['EbsSnapDocumentParameter'] = \
            os.environ['EBSSnapshotDocumentSSMParameter']
        return ebs_snap_manager(event, context.aws_request_id)
    if event['EventType'] == 'AutomationExecutionStatus':
        logger.info('Getting automation execution status for id: %s',
                    event['ExecutionData']['ExecutionId'])
        return get_execution_status(event)
    if event['EventType'] == 'FSxBackup':
        logger.info('Creating FSx Backup for FileSystem: %s',
                    event['FileSystemId'])
        return fsx_snap_manager(event, context.aws_request_id)
    if event['EventType'] == 'CheckBackupInProgress':
        logger.info('Checking for FSx Backups in Progress')
        return check_backup_status(event)
    if event['EventType'] == 'CleanupEbsSnapshots':
        logger.info('Cleaning up snapshots for instance: %s',
                    event['InstanceId'])
        ebs_snap_cleanup(event)
    if event['EventType'] == 'CleanupFSxBackups':
        logger.info('Cleaning up FSx backups for filesystem: %s',
                    event['FileSystemId'])
        fsx_backup_cleanup(event)


if __name__ == '__main__':
    lambda_handler(None, None)
