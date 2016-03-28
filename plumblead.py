# -*- coding: UTF-8 -*-
"""
Usage:
  plumblead [options]

Options:
  -h --help                   Show this screen.
  -c FILE --config-file=FILE  Path to a YAML configuration file [default: config.yaml].
  -i INTERVAL                 Interval, in ms, to wait between metric requests. Doubles as the backoff multiplier. [default: 50]
  -m MAX_INTERVAL             The maximum interval time to back off to, in ms [default: 4000]
  -p INT --period INT         Period length, in minutes [default: 1]
  -n INT                      Number of data points to try to get [default: 5]
  -v                          Verbose
  --version                   Show version.

This work-in-progress Elastic Beanstalk support is likely to change substantially, which is why it is not covered in
in documentation or tests.

"""

from tempfile import NamedTemporaryFile
import os

import boto
import boto.regioninfo

from leadbutt import leadbutt
from plumbum import get_jinja_template, get_template_tokens, interpret_options, CliArgsException


def list_beanstalk(region, environment_name_args):
    # args are named differently here as the use case is that we want attributes for exactly one environment
    region_info = boto.regioninfo.RegionInfo(None, region, 'elasticbeanstalk.{}.amazonaws.com'.format(region))
    eb_conn = boto.connect_beanstalk(
        region=region_info,
    )
    environments = eb_conn.describe_environment_resources(**environment_name_args)
    resources = environments['DescribeEnvironmentResourcesResponse']['DescribeEnvironmentResourcesResult']['EnvironmentResources']
    return resources


def main():
    template_file, namespace, region, filters, cli_tokens = interpret_options()

    # get the template first so this can fail before making a network request
    jinja_template = get_jinja_template(template_file)

    # this is the ugly hack part: we'll require the caller to pass in a specific CLI 'option'
    # to achieve the desired effect of getting the specified beanstalk environment resources
    # TODO: refactor plumbum into a class style, so an argument for the environment name can replace this
    if 'environment_name' not in filters:
        raise CliArgsException(
            'in {}, you must pass at least one environment_name=something filter'.format(
                os.path.basename(__file__)
            )
        )

    # check the namespace, this script only works for 'beanstall'
    if namespace != 'beanstalk':
        raise CliArgsException(
            "The only valid namespace for {} is 'beanstalk'".format(os.path.basename(__file__))
        )

    resources = list_beanstalk(region, filters)

    base_tokens = {
        'filters': filters,
        'region': region,  # Use for Auth config section if needed
        'resources': resources,
        'environment_name': filters['environment_name'],
    }

    tempfile = NamedTemporaryFile()
    tempfile.write(jinja_template.render(get_template_tokens(base_tokens=base_tokens, cli_tokens=cli_tokens)))
    tempfile.flush()
    # TODO: not hardcoding the Perdiod and Count requies a refactor of leabutt.py into a class with a config object.
    leadbutt(tempfile.name, {'Period': 1, 'Count': 5}, verbose=False)
    tempfile.close()
