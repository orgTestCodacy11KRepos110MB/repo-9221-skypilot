"""The module for AWS"""
from typing import Dict, List, Any, Optional

import copy
import logging

import botocore
from sky.provision.aws import utils

BOTO_CREATE_MAX_RETRIES = 5

# Tag uniquely identifying all nodes of a cluster
TAG_RAY_CLUSTER_NAME = 'ray-cluster-name'
# Tag for the name of the node
TAG_RAY_NODE_NAME = 'ray-node-name'

# Tag for user defined node types (e.g., m4xl_spot). This is used for multi
# node type clusters.
TAG_RAY_USER_NODE_TYPE = 'ray-user-node-type'

# Tag that reports the current state of the node (e.g. Updating, Up-to-date)
TAG_RAY_NODE_STATUS = 'ray-node-status'
STATUS_UNINITIALIZED = 'uninitialized'
STATUS_WAITING_FOR_SSH = 'waiting-for-ssh'
STATUS_SYNCING_FILES = 'syncing-files'
STATUS_SETTING_UP = 'setting-up'
STATUS_UPDATE_FAILED = 'update-failed'
STATUS_UP_TO_DATE = 'up-to-date'

# Hash of the node runtime config, used to determine if updates are needed
TAG_RAY_RUNTIME_CONFIG = 'ray-runtime-config'
# Hash of the contents of the directories specified by the file_mounts config
# if the node is a worker, this also hashes content of the directories
# specified by the cluster_synced_files config
TAG_RAY_FILE_MOUNTS_CONTENTS = 'ray-file-mounts-contents'

logger = logging.getLogger(__name__)

# ======================== About AWS subnet/VPC ========================
# https://stackoverflow.com/questions/37407492/are-there-differences-in-networking-performance-if-ec2-instances-are-in-differen
# https://docs.aws.amazon.com/vpc/latest/userguide/how-it-works.html
# https://docs.aws.amazon.com/vpc/latest/userguide/configure-subnets.html

# ======================== Instance state and lifecycle ========================
# https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-lifecycle.html

# Data transfer within the same region but different availability zone costs $0.01/GB:
# https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer_within_the_same_AWS_Region


def describe_instances(region: str) -> Dict:
    # overhead: 658 ms ± 65.3 ms
    return utils.create_ec2_client(region).describe_instances()


def _format_tags(tags: Dict[str, str]) -> List:
    return [{'Key': k, 'Value': v} for k, v in tags.items()]


def _merge_tag_specs(tag_specs: List[Dict[str, Any]],
                     user_tag_specs: List[Dict[str, Any]]) -> None:
    """Merges user-provided node config tag specifications into a base
    list of node provider tag specifications. The base list of
    node provider tag specs is modified in-place.

    This allows users to add tags and override values of existing
    tags with their own, and only applies to the resource type
    'instance'. All other resource types are appended to the list of
    tag specs.

    Args:
        tag_specs (List[Dict[str, Any]]): base node provider tag specs
        user_tag_specs (List[Dict[str, Any]]): user's node config tag specs
    """

    for user_tag_spec in user_tag_specs:
        if user_tag_spec['ResourceType'] == 'instance':
            for user_tag in user_tag_spec['Tags']:
                exists = False
                for tag in tag_specs[0]['Tags']:
                    if user_tag['Key'] == tag['Key']:
                        exists = True
                        tag['Value'] = user_tag['Value']
                        break
                if not exists:
                    tag_specs[0]['Tags'] += [user_tag]
        else:
            tag_specs += [user_tag_spec]


def create_instances(region: str, cluster_name: str, node_config: Dict[str,
                                                                       Any],
                     tags: Dict[str, str], count: int) -> Dict[str, Any]:
    ec2_fail_fast = utils.create_ec2_resource(region=region, max_attempts=0)

    tags = {'Name': cluster_name, TAG_RAY_CLUSTER_NAME: cluster_name, **tags}
    conf = node_config.copy()

    tag_specs = [{
        'ResourceType': 'instance',
        'Tags': _format_tags(tags),
    }]
    user_tag_specs = conf.get('TagSpecifications', [])
    _merge_tag_specs(tag_specs, user_tag_specs)

    # SubnetIds is not a real config key: we must resolve to a
    # single SubnetId before invoking the AWS API.
    subnet_ids = conf.pop('SubnetIds')

    # update config with min/max node counts and tag specs
    conf.update({
        'MinCount': 1,
        'MaxCount': count,
        'TagSpecifications': tag_specs
    })

    # NOTE: This ensures that we try ALL availability zones before
    # throwing an error.
    max_tries = max(BOTO_CREATE_MAX_RETRIES, len(subnet_ids))
    for i in range(max_tries):
        try:
            if 'NetworkInterfaces' in conf:
                # remove security group IDs previously copied from network
                # interfaces (create_instances call fails otherwise)
                conf.pop('SecurityGroupIds', None)
            else:
                # Launch failure may be due to instance type availability in
                # the given AZ. Try to always launch in the first listed subnet.
                subnet_id = subnet_ids[i % len(subnet_ids)]
                conf['SubnetId'] = subnet_id

            created = ec2_fail_fast.create_instances(**conf)
            return {n.id: n for n in created}
        except botocore.exceptions.ClientError as exc:
            if (i + 1) >= max_tries:
                raise RuntimeError(
                    'Failed to launch instances. Max attempts exceeded.'
                ) from exc
            else:
                logger.warning(
                    f'create_instances: Attempt failed with {exc}, retrying.')


def resume_instances(region: str,
                     cluster_name: str,
                     tags: Dict[str, str],
                     count: Optional[int] = None) -> Dict[str, Any]:
    ec2 = utils.create_ec2_resource(region=region)
    filters = [
        {
            'Name': 'instance-state-name',
            'Values': ['stopped', 'stopping'],
        },
        {
            'Name': f'tag:{TAG_RAY_CLUSTER_NAME}',
            'Values': [cluster_name],
        },
    ]
    reuse_nodes = list(ec2.instances.filter(Filters=filters))
    if count is not None:
        reuse_nodes = reuse_nodes[:count]
    reuse_node_ids = [n.id for n in reuse_nodes]
    if reuse_nodes:
        for node in reuse_nodes:
            if node.state['Name'] == 'stopping':
                node.wait_until_stopped()

        ec2.meta.client.start_instances(InstanceIds=reuse_node_ids)
        if tags:
            # empty tags will result in error in the API call
            ec2.meta.client.create_tags(
                Resources=reuse_node_ids,
                Tags=_format_tags(tags),
            )
    return {n.id: n for n in reuse_nodes}


def create_or_resume_instances(region: str, cluster_name: str,
                               node_config: Dict[str, Any],
                               tags: Dict[str, str], count: int,
                               resume_stopped_nodes: bool) -> Dict[str, Any]:
    """Creates instances.

    Returns dict mapping instance id to ec2.Instance object for the created
    instances.
    """
    # TODO(suquark): should we also check if there are running instances?
    # sort tags by key to support deterministic unit test stubbing
    tags = dict(sorted(copy.deepcopy(tags).items()))

    all_created_nodes = {}
    # Try to reuse previously stopped nodes with compatible configs
    if resume_stopped_nodes:
        all_created_nodes = resume_instances(region, cluster_name, tags, count)

    remaining_count = count - len(all_created_nodes)
    if remaining_count > 0:
        created_nodes_dict = create_instances(region, cluster_name, node_config,
                                              tags, remaining_count)
        all_created_nodes.update(created_nodes_dict)
    return all_created_nodes


def stop_instances(region: str, cluster_name: str):
    ec2 = utils.create_ec2_resource(region=region)
    filters = [
        {
            'Name': 'instance-state-name',
            'Values': ['pending', 'running'],
        },
        {
            'Name': f'tag:{TAG_RAY_CLUSTER_NAME}',
            'Values': [cluster_name],
        },
    ]
    ec2.instances.filter(Filters=filters).stop()


def terminate_instances(region: str, cluster_name: str):
    ec2 = utils.create_ec2_resource(region=region)
    filters = [
        {
            'Name': 'instance-state-name',
            # exclude 'shutting-down' or 'terminated' states
            'Values': ['pending', 'running', 'stopping', 'stopped'],
        },
        {
            'Name': f'tag:{TAG_RAY_CLUSTER_NAME}',
            'Values': [cluster_name],
        },
    ]
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Instance
    ec2.instances.filter(Filters=filters).terminate()


def wait_instances(region: str, cluster_name: str, state: str):
    # possible exceptions: https://github.com/boto/boto3/issues/176
    ec2 = utils.create_ec2_resource(region=region)
    client = ec2.meta.client

    filters = [
        {
            'Name': f'tag:{TAG_RAY_CLUSTER_NAME}',
            'Values': [cluster_name],
        },
    ]

    if state != 'terminated':
        # NOTE: there could be a terminated AWS cluster with the same cluster name.
        # Wait the cluster result in errors (cannot wait for 'terminated').
        # So here we exclude terminated instances.
        filters.append({
            'Name': 'instance-state-name',
            'Values': [
                'pending', 'running', 'shutting-down', 'stopping', 'stopped'
            ],
        })

    if state == 'running':
        waiter = client.get_waiter("instance_running")
    elif state == 'stopped':
        waiter = client.get_waiter("instance_stopped")
    elif state == 'terminated':
        waiter = client.get_waiter("instance_terminated")
    else:
        raise ValueError(f'Unsupported state to wait: {state}')
    # See https://github.com/boto/botocore/blob/develop/botocore/waiter.py
    waiter.wait(WaiterConfig={"Delay": 5}, Filters=filters)


def get_instance_ips(region: str, cluster_name: str):
    ec2 = utils.create_ec2_resource(region=region)
    filters = [
        {
            'Name': 'instance-state-name',
            'Values': ['running'],
        },
        {
            'Name': f'tag:{TAG_RAY_CLUSTER_NAME}',
            'Values': [cluster_name],
        },
    ]
    instances = ec2.instances.filter(Filters=filters)
    # TODO: use 'Name' in inst.tags instead of 'id'
    ips = [(inst.id, (inst.private_ip_address, inst.public_ip_address))
           for inst in instances]
    return dict(sorted(ips))
