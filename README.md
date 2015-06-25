# EC2 Auto Scaling with Ansible

We use Ansible to manage application deployments to EC2 with Auto Scaling. It's particularly suited because it lends itself to easy integration with existing processes such as CI, enabling rapid development of a continuous deployment pipeline. One crucial feature is that it is able to hand-hold a rolling deploy (that is, zero downtime) by terminating and replacing instances in batches. Typically when we deploy to EC2, we do so in an automated fashion which makes it important to have rollback capability and for this, we typically maintain a short history of Amazon Machine Images (AMIs) and Launch Configurations which are associated with a particular Auto Scaling Group (ASG). In the event you wish to roll back to a particular version of your application, you can simply associate your ASG with the previously known working launch configuration and replace all your instances.

Our normal workflow for auto scaling deployments starts with an Ansible playbook which runs through the deploy lifecycle. Each step along the way is represented by a role and applied in order, keeping the main playbook lean and configurable. Depending on our client's requirements, that playbook might be triggered in a number of ways such as the final step in a continuous integration build, or on demand via Hubot in a Slack/Flowdock/IRC chat.

In this post we'll walk through each stage of the build and deployment process, and use Ansible to perform all the work. The goal is to build our entire environment from scratch, save for a few manually created resources at the outset.


## Preparing AWS

We'll be using EC2 Classic for these examples, although they can be trivially adapted for VPC. Start by creating an EC2 Security Group for your application, taking care to open the necessary ports for your application in addition to TCP/22 for SSH.

Add a new keypair for SSH access to your instances. You can either create a new private/public keypair or upload your existing SSH public key.

You may optionally register and host a domain name with AWS Route 53. If you do so, the domain will be pointed at your application so that you don't have to browse to it by using an automatically assigned AWS hostname.


## Setting up Ansible

Ansible uses [Boto](https://github.com/boto/boto) for AWS interactions, so you'll need that installed on your control host. We're also going to make some use of the AWS CLI tools, so get those too. Your platform may differ, but the following will work for most platforms:

```bash
pip install python-boto awscli
```

We also assume Ansible 1.9.x, for Ubuntu you can get that from the Ansible PPA.

```bash
add-apt-repository ppa:ansible/ansible
apt-get install ansible
```

You should place your AWS access/secret keys into `~/.aws/credentials`

```ini
[Credentials]
aws_access_key_id = <your_access_key_here>
aws_secret_access_key = <your_secret_key_here>
```

We'll be using the ec2.py dynamic inventory script for Ansible so we can address our EC2 instances by various attributes instead of hard coding hostnames into an inventory file. It's not included with the Ubuntu distribution(s) of Ansible, so we'll grab it from GitHub. Place [ec2.py](https://raw.githubusercontent.com/ansible/ansible/stable-1.9/plugins/inventory/ec2.py) and [ec2.ini](https://raw.githubusercontent.com/ansible/ansible/stable-1.9/plugins/inventory/ec2.ini) into `/etc/ansible/inventory` (creating that directory if absent)

Modify `/etc/ansible/ansible.cfg` to use that directory as the inventory source:

```ini
# /etc/ansible/ansible.cfg
inventory = /etc/ansible/inventory
```


## Step 1: Launch a new EC2 instance

A prerequisite to setting up an application for auto scaling involves building an AMI containing your working application, which will be used to launch new instances to meet demand. We'll start by launching a new instance onto which we can deploy our application. Create the following files:

```yaml
---
# group_vars/all.yml

region: us-east-1
zone: us-east-1a
keypair: YOUR_KEYPAIR
security_groups: YOUR_SECURITY_GROUP
instance_type: m3.medium
volumes:
  - device_name: /dev/sda1
    device_type: gp2
    volume_size: 20
    delete_on_termination: true
```

```yaml
---
# deploy.yml

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build
```

```yaml
---
# roles/launch/tasks/main.yml

- name: Search for the latest Ubuntu 14.04 AMI
  ec2_ami_find:
    region: "{{ region }}"
    name: "ubuntu/images/hvm-ssd/ubuntu-trusty-14.04-amd64-server-*"
    owner: 099720109477
    sort: name
    sort_order: descending
    sort_end: 1
    no_result_action: fail
  register: ami_result

- name: Launch new instance
  ec2:
    region: "{{ region }}"
    keypair: "{{ keypair }}"
    zone: "{{ zone }}"
    group: "{{ security_groups }}"
    image: "{{ ami_result.results[0].ami_id }}"
    instance_type: "{{ instance_type }}"
    instance_tags:
      Name: "{{ name }}"
    volumes: "{{ volumes }}"
    wait: yes
  register: ec2

- name: Add new instances to host group
  add_host:
    name: "{{ item.public_dns_name }}"
    groups: "{{ name }}"
    ec2_id: "{{ item.id }}"
  with_items: ec2.instances

- name: Wait for instance to boot
  wait_for:
    host: "{{ item.public_dns_name }}"
    port: 22
    delay: 30
    timeout: 300
    state: started
  with_items: ec2.instances
```

The ec2_ami_find module is a new addition to Ansible 2.0 but has not been backported to 1.9, so we'll need to [import this module from GitHub](https://raw.githubusercontent.com/ansible/ansible-modules-core/devel/cloud/amazon/ec2_ami_find.py) and place it into the `library/` directory relative to `deploy.yml`.

Run the playbook with `ansible-playbook deploy.yml -vv` and a new instance will be launched. You'll see it in the AWS Web Console and you should be able to SSH to it.

## Step 2: Deploy the application

Now we'll use Ansible to deploy our application and start it. We'll deploy a sample Node.js web application, the source code of which is kept in a public git repository. Ansible is going to clone and checkout our application at a desired revision on the target instance and configure it to start on boot, in addition to setting up a web server.

```yaml
---
# deploy.yml

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx
```

```yaml
---
# roles/deploy/tasks/main.yml

- name: Install git
  apt:
    pkg: git
    state: present
  sudo: yes

- name: Create www directory
  file:
    path: /srv/www
    owner: ubuntu
    group: ubuntu
    state: directory
  sudo: yes

- name: Clone repository
  git:
    repo: "https://github.com/atplanet/hello-world-express-app.git"
    dest: /srv/www/webapp
    version: master

- name: Install upstart script
  copy:
    src: upstart.conf
    dest: /etc/upstart/webapp.conf
  sudo: yes

- name: Enable and start the application
  service:
    name: webapp
    enabled: yes
    state: restarted
  sudo: yes
```

```bash
# roles/deploy/files/upstart.conf

description "Sample Node.js app"
author "Tom Bamford"

start on (local-filesystems and net-device-up)
stop on runlevel [06]

env IP="127.0.0.1"
env NODE_ENV="production"
setuid ubuntu

respawn
exec node /srv/www/webapp/app.js
```

```yaml
---
# roles/nginx/tasks/main.yml

- name: Install Nginx
  apt:
    pkg: nginx
    state: present
  sudo: yes

- name: Configure Nginx
  copy:
    src: nginx.conf
    dest: /etc/sites-enabled/default
  sudo: yes

- name: Enable and start Nginx
  service:
    name: nginx
    enabled: yes
    state: restarted
  sudo: yes
```

```php
# roles/nginx/files/nginx.conf

server {
  listen 80 default_server;
  location / {
    proxy_pass http://127.0.0.1:8000;
  }
}
```

Running the playbook again will launch another instance, install some useful packages, deploy our application and set up Nginx as our web server. If you browse to the newest instance at its hostname, as reported in the output of ansible-playbook, you should see a "Hello World" page.

## Step 3: Build the AMI

Now that the application is deployed and running, we can use the newly launched instance to build an AMI. Create the `build-ami` role and amend the deploy.yml to invoke it.

```yaml
---
# deploy.yml

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - create-ami
```

```yaml
---
# roles/build-ami/tasks/main.yml
- name: Create AMI
  ec2_ami:
    region: "{{ region }}"
    instance_id: "{{ ec2_id }}"
    name: "webapp-{{ ansible_date_time.iso8601 | regex_replace('[^a-zA-Z0-9]', '-') }}"
    wait: yes
    state: present
  register: ami
```

## Step 4: Terminate old instances

You'll probably have noticed by now that each time the playbook is run, Ansible launches a new instance. At this rate, we'll keep accumulating instances that we don't need, so we will add another role and a new task to locate these instances and terminate them. Now, after Ansible successfully launches a new instance, it will terminate any existing instances immediately afterwards.

```yaml
---
# deploy.yml

- name: Find existing instance(s)
  hosts: "tag_Name_ami-build"
  gather_facts: false
  tags: find
  tasks:
    - name: Add to old-ami-build group
      group_by:
        key: old-ami-build

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - create-ami

- hosts: old-ami-build
  roles:
    - terminate
```

```yaml
---
# roles/terminate/tasks/main.yml
- name: Terminate old instance(s)
  ec2:
    instance_ids: "{{ ec2_id }}"
    region: "{{ region }}"
    state: absent
    wait: yes
```

## Step 5: Create a Launch Configuration

Our AMI is built, so now we'll want to create a new Launch Configuration to describe the new instances that should be launched from this AMI. We'll create another role to handle that.

```yaml
---
# deploy.yml

- name: Find existing instance(s)
  hosts: "tag_Name_ami-build"
  gather_facts: false
  tags: find
  tasks:
    - name: Add to old-ami-build group
      group_by:
        key: old-ami-build

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - create-ami
    - create-launch-configuration

- hosts: old-ami-build
  roles:
    - terminate
```

```yaml
---
# roles/create-launch-configuration/tasks/main.yml

- name: Create Launch Configuration
  ec2_lc:
    region: "{{ region }}"
    name: "webapp-{{ ansible_date_time.iso8601 | regex_replace('[^a-zA-Z0-9]', '-') }}"
    image_id: "{{ ami.image_id }}"
    key_name: "{{ keypair }}"
    instance_type: "{{ instance_type }}"
    security_groups: "{{ security_groups }}"
    volumes: "{{ volumes }}"
    instance_monitoring: yes
```

## Step 6: Create an Elastic Load Balancer

Clients will connect to an Elastic Load Balancer which will distribute incoming requests among the instances we have launched into our upcoming Auto Scaling Group. Again we'll create another role to handle the management of the ELB, and apply it from our playbook.

```yaml
---
# deploy.yml

- name: Find existing instance(s)
  hosts: "tag_Name_ami-build"
  gather_facts: false
  tags: find
  tasks:
    - name: Add to old-ami-build group
      group_by:
        key: old-ami-build

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - create-ami
    - create-launch-configuration
    - load-balancer

- hosts: old-ami-build
  roles:
    - terminate
```

```yaml
---
# roles/load-balancer/tasks/main.yml

- name: Configure Elastic Load Balancers
  ec2_elb_lb:
    region: "{{ region }}"
    name: webapp
    state: present
    zones: "{{ zone }}"
    connection_draining_timeout: 60
    listeners:
      - protocol: http
        load_balancer_port: 80
        instance_port: 80
    health_check:
      ping_protocol: http
      ping_port: 80
      ping_path: "/"
      response_timeout: 10
      interval: 30
      unhealthy_threshold: 6
      healthy_threshold: 2
  register: elb_result
```

## Step 7: Create and configure an Auto Scaling Group

We'll create an Auto Scaling Group and configure it to use the Launch Configuration we previously created. Within the boundaries that we define, AWS will launch instances into the ASG dynamically based on the current load across all instances. Equally when the load drops, some instances will be terminated accordingly. Exactly how many instances are launched or terminated is defined in one or more scaling policies, which are also created and linked to the ASG.

```yaml
---
# deploy.yml

- name: Find existing instance(s)
  hosts: "tag_Name_ami-build"
  gather_facts: false
  tags: find
  tasks:
    - name: Add to old-ami-build group
      group_by:
        key: old-ami-build

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - create-ami
    - create-launch-configuration
    - load-balancer
    - auto-scaling

- hosts: old-ami-build
  roles:
    - terminate
```

```yaml
---
# roles/auto-scaling/tasks/main.yml

- name: Retrieve current Auto Scaling Group properties
  command: "aws --region {{ region }} autoscaling describe-auto-scaling-groups --auto-scaling-group-names webapp"
  register: asg_properties_result

- name: Set asg_properties variable from JSON output if the Auto Scaling Group already exists
  set_fact:
    asg_properties: "{{ (asg_properties_result.stdout | from_json).AutoScalingGroups[0] }}"
  when: (asg_properties_result.stdout | from_json).AutoScalingGroups | count

- name: Configure Auto Scaling Group and perform rolling deploy
  ec2_asg:
    region: "{{ region }}"
    name: webapp
    launch_config_name: webapp
    availability_zones: "{{ zone }}"
    health_check_type: ELB
    health_check_period: 300
    desired_capacity: "{{ asg_properties.DesiredCapacity | default(2) }}"
    replace_all_instances: yes
    replace_batch_size: "{{ (asg_properties.DesiredCapacity | default(2) / 4) | round(0, 'ceil') | int }}"
    min_size: 2
    max_size: 10
    load_balancers:
      - webapp
    state: present
  register: asg_result

- name: Configure Scaling Policies
  ec2_scaling_policy:
    region: "{{ region }}"
    name: "{{ item.name }}"
    asg_name: webapp
    state: present
    adjustment_type: "{{ item.adjustment_type }}"
    min_adjustment_step: "{{ item.min_adjustment_step }}"
    scaling_adjustment: "{{ item.scaling_adjustment }}"
    cooldown: "{{ item.cooldown }}"
  with_items:
    - name: "Increase Group Size"
      adjustment_type: "ChangeInCapacity"
      scaling_adjustment: +1
      min_adjustment_step: 1
      cooldown: 180
    - name: "Decrease Group Size"
      adjustment_type: "ChangeInCapacity"
      scaling_adjustment: -1
      min_adjustment_step: 1
      cooldown: 300
  register: sp_result

- name: Determine Metric Alarm configuration
  set_fact:
    metric_alarms:
      - name: "{{ asg_name }}-ScaleUp"
        comparison: ">="
        threshold: 50.0
        alarm_actions:
          - "{{ sp_result.results[0].arn }}"
      - name: "{{ asg_name }}-ScaleDown"
        comparison: "<="
        threshold: 20.0
        alarm_actions:
          - "{{ sp_result.results[1].arn }}"

- name: Configure Metric Alarms and link to Scaling Policies
  ec2_metric_alarm:
    region: "{{ region }}"
    name: "{{ item.name }}"
    state: present
    metric: "CPUUtilization"
    namespace: "AWS/EC2"
    statistic: "Average"
    comparison: "{{ item.comparison }}"
    threshold: "{{ item.threshold }}"
    period: 60
    evaluation_periods: 5
    unit: "Percent"
    dimensions:
      AutoScalingGroupName: "{{ asg_name }}"
    alarm_actions: "{{ item.alarm_actions }}"
  with_items: metric_alarms
  when: max_size > 1
  register: ma_result
```

There's more going on here too. We not only configure our ASG and scaling policies, but also create CloudWatch metric alarms to measure the load across our instances, and associate them with the corresponding scaling policies to complete our configuration.

Here we have configured our CloudWatch alarms to trigger based on aggregate CPU usage within our auto scaling group. When the average CPU utilization exceeds 50% across your instances for 5 consecutive samples taken every 60 seconds (i.e. 5 minutes), a scaling event will be triggered that launches a new instance to relieve the load. A corresponding CloudWatch alarm also triggers a scaling event to terminate an instance from the auto scaling group when the average CPU utilization drops below 20% across your instances for the same sample period.

The minimum and maximum sizes for the auto scaling group are set to 2 and 10 respectively. It's important to get these values right for your application workload. You do not want to be under resourced for early peaks in traffic, and for redundancy reasons it's a good idea to always have at least 2 instances in service. Equally you probably want your application to scale for peak periods, but perhaps not beyond a safety limit in the event you receive massive amounts of traffic which could result in escalating costs.

Particularly important to note here is how we configure the `ec2_asg` module to perform rolling deploys. First, we determine how many instances the ASG currently has running and use this to specify our `desired_capacity` and calculate a suitable `replace_batch_size`. The `replace_all_instances` option specifies that all currently running instances should be replaced by new instances using the new Launch Configuration. Together, this ensures that the capacity of our ASG is not adversely affected during the deploy and allows us to safely deploy at any time, whether we are currently running 5 or 5000 instances! Of course this means that the more instances you have running, the longer the entire process will take. You may wish to increase the `replace_batch_size` if you are consistently running more instances.

## Step 8: Update DNS (optional)

If you have a domain name, or subdomain, set up with AWS Route 53, you can have Ansible update the DNS records to point to your Auto Scaling Group.

```yaml
---
# deploy.yml

- name: Find existing instance(s)
  hosts: "tag_Name_ami-build"
  gather_facts: false
  tags: find
  tasks:
    - name: Add to old-ami-build group
      group_by:
        key: old-ami-build

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - create-ami
    - create-launch-configuration
    - load-balancer
    - auto-scaling
    - dns

- hosts: old-ami-build
  roles:
    - terminate
```

```yaml
---
# roles/dns/tasks/main.yml

- name: Update DNS
  route53:
    command: create
    overwrite: yes
    zone: "{{ domain }}"
    record: "www.{{ domain }}"
    type: CNAME
    ttl: 300
    value: "{{ elb_result.elb.dns_name }}"
```

## Step 9: Cleaning up

Whilst we already configured Ansible to terminate old instances used for building AMIs, right now we will start to accumulate launch configurations and AMIs each time we invoke the `deploy.yml` playbook. This might not appear to be much of a problem at the outset (financial costs aside), but it will soon become an issue due to service limits imposed by AWS. At the time of writing, the relevant limit on Launch Configurations was 100 per region. When this limit is reached, no more can be created and our playbook will start to fail.

Note that whilst you can request increased limits per region for your account, in our experience sometimes these requests are refused on the grounds that AWS would prefer for you to clean up your cruft instead of relying on perpetual service limit increases.

Leaving unused resources lying around is not very good practise in any case, and we certainly don't want to be paying for those resources unnecessarily. To fix this, we'll make use of the `ec2_ami_find`/`ec2_ami` modules to delete the older AMIs, and a quick and dirty (but effective) hand rolled module to discard old launch configurations.

```yaml
---
# deploy.yml

- name: Find existing instance(s)
  hosts: "tag_Name_ami-build"
  gather_facts: false
  tags: find
  tasks:
    - name: Add to old-ami-build group
      group_by:
        key: old-ami-build

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - role: launch
      name: ami-build

- hosts: ami-build
  roles:
    - deploy
    - nginx

- hosts: ami-build
  connection: local
  gather_facts: no
  roles:
    - create-ami
    - create-launch-configuration
    - load-balancer
    - auto-scaling
    - dns

- hosts: localhost
  connection: local
  gather_facts: no
  roles:
    - delete-old-launch-configurations
    - delete-old-amis

- hosts: old-ami-build
  connection: local
  gather_facts: no
  roles:
    - terminate
```

```yaml
---
# roles/delete-old-amis/tasks/main.yml

- ec2_ami_find:
    region: "{{ region }}"
    owner: self
    name: "webapp-*"
    sort: name
    sort_end: -10
  register: old_ami_result

- ec2_ami:
    region: "{{ region }}"
    image_id: "{{ item.ami_id }}"
    delete_snapshot: yes
    state: absent
  with_items: old_ami_result.results
  ignore_errors: yes
```

```yaml
---
# roles/delete-old-launch-configurations/tasks/main.yml

- lc_find:
    region: "{{ region }}"
    name_regex: "webapp-.*"
    sort: yes
    sort_end: -10
  register: old_lc_result

- ec2_lc:
    region: "{{ region }}"
    name: "{{ item.name }}"
    state: absent
  with_items: old_lc_result.results
  ignore_errors: yes
```

```python
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
```

When these roles are used together, Ansible will maintain a history of 10 AMIs and 10 Launch Configurations prior to the latest one of each. This will provide our rollback capability; in the event that you wish to roll back to an earlier deployed version of your application, you can update the active Launch Configuration in your Auto Scaling Group settings and replace your instances by terminating them in batches. Auto Scaling will start up new instances with your specified launch configuration in order to fulfill the desired instance count.


## Win!

Now that we have a completed playbook to handle deployments of our application to EC2 Auto Scaling, all that remains is to hook it up to your existing systems to invoke it whenever you want a new deploy to occur. We'll cover that in a later blog post.

All the code from this article is available [on GitHub](https://github.com/atplanet/ansible-auto-scaling-tutorial).
