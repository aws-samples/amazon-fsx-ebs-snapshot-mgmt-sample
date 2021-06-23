#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

#! /usr/bin/env python3
"""
This lambda discovers the FSx file systems and EC2 instances based upon
specific tags and invokes and passes that information to an AWS Step Function
"""

import sys
import os
import json
import logging
import boto3
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


def describe_ec2_instances(ec2, ec2_filter):
    """Describe and return ec2 instances with specific tags."""
    tmp_instances = []
    instances = []
    resp = ec2.describe_instances(Filters=ec2_filter)
    for res in resp['Reservations']:
        tmp_instances.extend(res['Instances'])
    while 'NextToken' in resp:
        resp = ec2.describe_instances(Filters=ec2_filter,
                                      NextToken=resp['NextToken'])
        for res in resp['Reservations']:
            tmp_instances.extend(res['Instances'])

    for inst in tmp_instances:
        instances.append({'InstanceId': inst['InstanceId'],
                          'State': inst['State'],
                          'BlockDeviceMappings': inst['BlockDeviceMappings'],
                          'AttemptCount': 0,
                          'Tags': inst['Tags']})
    return instances


def describe_filesystems(fsx):
    """Describe all FSx filesystems."""
    f_systems = []
    resp = fsx.describe_file_systems()
    f_systems.extend(resp['FileSystems'])
    while 'NextToken' in resp:
        resp = fsx.describe_file_systems(NextToken=resp['NextToken'])
        f_systems.extend(resp['FileSystems'])
    return f_systems


def parse_filesystems(f_systems, tag_key, tag_val):
    """Parse filesystems by tag."""
    tmp_fsystems = []
    for f_system in f_systems:
        if f_system['Lifecycle'] == 'AVAILABLE':
            for tag in f_system['Tags']:
                if ((tag['Key'] == tag_key) and (tag['Value'] == tag_val)):
                    tmp_fsystems.append(
                        {'FileSystemId': f_system['FileSystemId'],
                         'Tags': f_system['Tags'],
                         'AttemptCount': 0,
                         'Lifecycle': f_system['Lifecycle']})
    return tmp_fsystems


def lambda_handler(event, context):
    """Invoke the lambda function."""
    # Create the required objects and variables
    ec2 = boto3.client('ec2')
    fsx = boto3.client('fsx')
    sfn = boto3.client('stepfunctions')
    cfn_rt_key = os.environ['EBSResourceTypeTag']
    cfn_rt_val = os.environ['EBSResourceTypeValue']
    cfn_frt_key = os.environ['FSxResourceTypeTag']
    cfn_frt_val = os.environ['FSxResourceTypeValue']
    sfn_arn = os.environ['StepFunctionArn']

    # Get the ec2 instances with the appropriate tags
    ec2_filter = [
        {'Name': 'tag:{}' .format(cfn_rt_key),
         'Values': [cfn_rt_val]},
        {'Name': 'instance-state-name', 'Values': ['running']}
    ]
    tagged_instances = describe_ec2_instances(ec2, ec2_filter)

    # Get the FSx Filesystems with the appropriate tags
    all_fsystems = describe_filesystems(fsx)
    tagged_fsystems = parse_filesystems(all_fsystems, cfn_frt_key, cfn_frt_val)

    if not tagged_instances:
        print('No tagged instances found')
    else:
        print('Tagged Instances: {}' .format(tagged_instances))
    if not tagged_fsystems:
        print('No tagged filesystems found')
    else:
        print('Tagged FileSystems: {}' .format(tagged_fsystems))
    if ((not tagged_instances) and (not tagged_fsystems)):
        print('No tagged resources found, nothing to create snapshots of')
        sys.exit()

    sfn_input = {}
    # Create the sql stacks json obj
    sfn_input['Ec2Instances'] = tagged_instances
    # Create the fsx stacks json obj
    sfn_input['FSxFileSystems'] = tagged_fsystems
    sfn.start_execution(stateMachineArn=sfn_arn,
                        input=json.dumps(jsonify(sfn_input)))
    return None


if __name__ == '__main__':
    lambda_handler(None, None)
