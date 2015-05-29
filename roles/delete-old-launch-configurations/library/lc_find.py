#!/usr/bin/python

# roles/delete-old-launch-configurations/library/lc_find.py

import json
import subprocess

def main():
    argument_spec = ec2_argument_spec()
    argument_spec.update(dict(
            region = dict(required=True,
                aliases = ['aws_region', 'ec2_region']),
            name_regex = dict(required=False),
            sort = dict(required=False, default=None, type='bool'),
            sort_order = dict(required=False, default='ascending',
                choices=['ascending', 'descending']),
            sort_start = dict(required=False),
            sort_end = dict(required=False),
        )
    )
    module = AnsibleModule(
        argument_spec=argument_spec,
    )
    name_regex = module.params.get('name_regex')
    sort = module.params.get('sort')
    sort_order = module.params.get('sort_order')
    sort_start = module.params.get('sort_start')
    sort_end = module.params.get('sort_end')
    lc_cmd_result = subprocess.check_output(["aws", "autoscaling", "describe-launch-configurations", "--region",  module.params.get('region')])
    lc_result = json.loads(lc_cmd_result)
    results = []
    for lc in lc_result['LaunchConfigurations']:
        data = {
            'arn': lc["LaunchConfigurationARN"],
            'name': lc["LaunchConfigurationName"],
        }
        results.append(data)
    if name_regex:
        regex = re.compile(name_regex)
        results = [result for result in results if regex.match(result['name'])]
    if sort:
        results.sort(key=lambda e: e['name'], reverse=(sort_order=='descending'))
    try:
        if sort and sort_start and sort_end:
            results = results[int(sort_start):int(sort_end)]
        elif sort and sort_start:
            results = results[int(sort_start):]
        elif sort and sort_end:
            results = results[:int(sort_end)]
    except TypeError:
        module.fail_json(msg="Please supply numeric values for sort_start and/or sort_end")
    module.exit_json(results=results)

from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

if __name__ == '__main__':
    main()
