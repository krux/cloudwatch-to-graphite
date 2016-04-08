# -*- coding: UTF-8 -*-
"""
Usage:
  leadbutt [options]

Options:
  -h --help                   Show this screen.
  -c FILE --config-file=FILE  Path to a YAML configuration file [default: config.yaml].
  -i INTERVAL                 Interval, in ms, to wait between metric requests. Doubles as the backoff multiplier. [default: 50]
  -m MAX_INTERVAL             The maximum interval time to back off to, in ms [default: 4000]
  -p INT --period INT         Period length, in minutes [default: 1]
  -n INT                      Number of data points to try to get [default: 5]
  -v                          Verbose
  --version                   Show version.
"""
from __future__ import unicode_literals

from calendar import timegm
import datetime
import os.path
import sys
import time
import ast

from docopt import docopt
import boto.ec2.cloudwatch
import boto.logs
from retrying import retry
import yaml


# emulate six.text_type based on https://docs.python.org/3/howto/pyporting.html#str-unicode
if sys.version_info[0] >= 3:
    text_type = str
else:
    text_type = unicode

__version__ = '0.9.5b2'


# configuration

DEFAULT_REGION = 'us-east-1'

DEFAULT_OPTIONS = {
    'Period': 1,  # 1 minute
    'Count': 5,  # 5 periods
    'Formatter': 'cloudwatch.%(Namespace)s.%(dimension)s.%(MetricName)s.%(statistic)s.%(Unit)s'
}


def get_config(config_file):
    """Get configuration from a file."""
    def load(fp):
        try:
            return yaml.load(fp)
        except yaml.YAMLError as e:
            sys.stderr.write(text_type(e))
            sys.exit(1)  # TODO document exit codes

    if config_file == '-':
        return load(sys.stdin)
    if not os.path.exists(config_file):
        sys.stderr.write('ERROR: Must either run next to config.yaml or specify a config file.\n' + __doc__)
        sys.exit(2)
    with open(config_file) as fp:
        return load(fp)


def get_options(config_options, local_options, cli_options):
    """
    Figure out what options to use based on the four places it can come from.

    Order of precedence:
    * cli_options      specified by the user at the command line
    * local_options    specified in the config file for the metric
    * config_options   specified in the config file at the base
    * DEFAULT_OPTIONS  hard coded defaults
    """
    options = DEFAULT_OPTIONS.copy()
    if config_options is not None:
        options.update(config_options)
    if local_options is not None:
        options.update(local_options)
    if cli_options is not None:
        options.update(cli_options)
    return options


def log_list_map(category, statistic_dict):
    list_map = {
        "network": "interface",
        "diskIO": "device",
        "fileSys": "name",
        "processList": "name"
    }
    # return list_map.get(statistic_dict[category], index)
    return statistic_dict[list_map.get(category)]


def unit_type_map(category, statistic):
    unit_map = {
        "cpuUtilization": {
            "guest": "Percent",
            "irq": "Percent",
            "system": "Percent",
            "wait": "Percent",
            "idle": "Percent",
            "user": "Percent",
            "total": "Percent",
            "steal": "Percent",
            "nice": "Percent",
        },
        "loadAverageMinute": {
            "fifteen": "Count",
            "five": "Count",
            "one": "Count",
        },
        "memory": {
            "writeback": "Kilobytes",
            "hugePagesFree": "Count",
            "hugePagesRsvd": "Count",
            "hugePagesSurp": "Count",
            "hugePagesTotal": "Count",
            "cached": "Kilobytes",
            "hugePagesSize": "Kilobytes",
            "pageTables": "Kilobytes",
            "dirty": "Kilobytes",
            "mapped": "Kilobytes",
            "active": "Kilobytes",
            "total": "Kilobytes",
            "slab": "Kilobytes",
            "buffers": "Kilobytes",
        },
        "tasks": {
            "sleeping": "Count",
            "zombie": "Count",
            "running": "Count",
            "stopped": "Count",
            "total": "Count",
            "blocked": "Count"
        },
        "swap": {
            "cached": "Kilobytes",
            "total": "Kilobytes",
            "free": "Kilobytes"
        },
        "network":
        {
            "rx": "Bytes/Second",
            "tx": "Bytes/Second"
        },
        "diskIO":
        {
            "writeKbPS": "Kilobytes/Second",
            "readIOsPS": "IO/Second",
            "await": "Milliseconds",
            "readKbPS": "Kilobytes/Second",
            "rrqmPS": "IO/Second",
            "util": "Percent",
            "avgQueueLen": "Milliseconds",
            "tps": "Count",
            "readKb": "Kilobytes",
            "writeKb": "Kilobytes",
            "avgReqSz": "Kilobytes",
            "wrqmPS": "IO/Second",
            "writeIOsPS": "IO/Second"
        },
        "fileSys":
        {
            "used": "Kilobytes",
            "usedFiles": "Count",
            "usedFilePercent": "Percent",
            "maxFiles": "Count",
            "total": "Kilobytes",
            "usedPercent": "Percent"
        },
        "processList":
        {
            "vss": "Kilobytes",
            "tgid": "Count",
            "parentID": "Count",
            "memoryUsedPc": "Percent",
            "cpuUsedPc": "Percent",
            "id": "Count",
            "rss": "Kilobytes"
        }
    }
    return unit_map[category].get(statistic, 'Count')


def output_log_results(formatter, ingestion_time, context, value):
    metric_name = (formatter % context).replace('/', '.').lower()
    line = '{0} {1} {2}\n'.format(
        metric_name,
        value,
        ingestion_time,
    )
    sys.stdout.write(line)


def process_log_results(results, options):
    """
    Output CW enhanced Monitoring to stdout.

    http://boto.cloudhackers.com/en/latest/ref/logs.html
    """

    context = {}
    # iterate over each result
    for result in results:
        # timestamp when metric arrived at queue
        ingestion_time = result['ingestionTime'] / 1000
        message = ast.literal_eval(result['message'])
        context['Dimension'] = message['instanceID']
        context['Namespace'] = 'AWS/RDS'
        # iterate over category keys example: ["cpuUtilization", "memory"]
        for category in message.keys():
            context['MetricName'] = category
            statistics = message[category]
            statistics_type = type(statistics)
            # process logs if value is a dictionary
            if statistics_type is dict:
                for statistic in statistics.keys():
                    context['Statistic'] = statistic
                    value = statistics[statistic]
                    if type(value) is int or type(value) is float:
                        # determine unit type example (Bytes/Second, Percent,
                        # Count)
                        context['Unit'] = unit_type_map(
                            category, statistic)
                        # output log to stdout for netcat to pickup
                        output_log_results(options['Formatter'], ingestion_time, context, value)
            # process list values differently, because of sub types
            elif statistics_type is list:
                for statistic_dict in statistics:
                    statistic_list = statistic_dict.keys()
                    # determine sub type using static map
                    context['ListCategory'] = log_list_map(
                        category, statistic_dict)
                    for statistic in statistic_list:
                        context['Statistic'] = statistic
                        value = statistic_dict[statistic]
                        # make sure value is a integer
                        if type(value) is int or type(value) is float:
                            # determine unit type example (Bytes/Second,
                            # Percent, Count)
                            context['Unit'] = unit_type_map(
                                category, statistic)
                            # output log to stdout for netcat to pickup
                            output_log_results(options['ListFormatter'], ingestion_time, context, value)


def output_results(results, metric, options):
    """
    Output the results to stdout.

    TODO: add AMPQ support for efficiency
    """
    formatter = options['Formatter']
    context = metric.copy()  # XXX might need to sanitize this
    try:
        context['dimension'] = list(metric['Dimensions'].values())[0]
    except AttributeError:
        context['dimension'] = ''
    for result in results:
        stat_keys = metric['Statistics']
        if not isinstance(stat_keys, list):
            stat_keys = [stat_keys]
        for statistic in stat_keys:
            context['statistic'] = statistic
            # get and then sanitize metric name, first copy the unit name from the
            # result to the context to keep the default format happy
            context['Unit'] = result['Unit']
            metric_name = (formatter % context).replace('/', '.').lower()
            line = '{0} {1} {2}\n'.format(
                metric_name,
                result[statistic],
                timegm(result['Timestamp'].timetuple()),
            )
            sys.stdout.write(line)


def value_pad_results(results, start_time, end_time, interval, value=0):
    """
    Pad CloudWatch results with a default value.

    For a set of CloudWatch API results, check if there is a result at each timestamp results are expected;
    where absent, set it to the 'value' parameter. Return the padded set of results. Start and end times
    need to have the microseconds shaved to match what the CloudWatch API returns.
    :param results: the result set returned by get_metric_statistics
    :param start_time: as passed to get_metric_statistics
    :param end_time: as passed to get_metric_statistics
    :param interval: the interval *in minutes* at which results are expected
    :param value: the value to put in the results
    :return:
    """
    this_time = start_time - datetime.timedelta(microseconds=start_time.microsecond)
    end_time = end_time - datetime.timedelta(microseconds=end_time.microsecond)
    while this_time < end_time:
        if not filter(lambda x: x['Timestamp'] == this_time, results):
            results.append({
                'Timestamp': this_time,
                'Sum': value,
                'Unit': 'Count',
            })
        this_time += datetime.timedelta(seconds=interval * 60)
    return results


def leadbutt(config_file, cli_options, verbose=False, **kwargs):

    # This function is defined in here so that the decorator can take CLI options, passed in from main()
    # we'll re-use the interval to sleep at the bottom of the loop that calls get_metric_statistics.
    @retry(wait_exponential_multiplier=kwargs.get('interval', None),
           wait_exponential_max=kwargs.get('max_interval', None),
           # give up at the point the next cron of this script probably runs;
           # Period is minutes; some_max_delay needs ms
           stop_max_delay=cli_options['Count'] * cli_options['Period'] * 60 * 1000)
    def get_metric_statistics(**kwargs):
        """
        A thin wrapper around boto.cloudwatch.connection.get_metric_statistics, for the
        purpose of adding the @retry decorator
        :param kwargs:
        :return:
        """
        connection = kwargs.pop('connection')
        return connection.get_metric_statistics(**kwargs)

    def get_logs_statistics(**kwargs):
        """
        A thin wrapper around boto.logs.get_log_events, for the
        purpose of adding the @retry decorator
        :param kwargs:
        :return:
        """
        connection = kwargs.pop('connection')
        return connection.get_log_events(**kwargs)

    config = get_config(config_file)
    config_options = config.get('Options')
    auth_options = config.get('Auth', {})
    enhanced_monitoring = config.get('EnhancedMonitoring', False)

    region = auth_options.get('region', DEFAULT_REGION)
    connect_args = {
        'debug': 2 if verbose else 0,
    }
    if 'aws_access_key_id' in auth_options:
        connect_args['aws_access_key_id'] = auth_options['aws_access_key_id']
    if 'aws_secret_access_key' in auth_options:
        connect_args['aws_secret_access_key'] = auth_options['aws_secret_access_key']
    conn = boto.ec2.cloudwatch.connect_to_region(region, **connect_args)
    if 'Metrics' in config:
        for metric in config['Metrics']:
            options = get_options(
                config_options, metric.get('Options'), cli_options)
            period_local = options['Period'] * 60
            count_local = options['Count']
            # if you have metrics that are available only every 5 minutes, be sure to request only stats
            # that are likely/sure to be up to date, ie ones ending on the previous
            # period increment.
            end_time = datetime.datetime.utcnow() - datetime.timedelta(seconds=int(time.time()) % period_local)
            start_time = end_time - datetime.timedelta(seconds=period_local * count_local)

            # if 'Unit 'is in the config, request only that; else get all units
            unit = metric.get('Unit')
            metric_names = metric['MetricName']
            if not isinstance(metric_names, list):
                metric_names = [metric_names]
            for metric_name in metric_names:
                # we need a copy of the metric dict with the MetricName swapped out
                this_metric = metric.copy()
                this_metric['MetricName'] = metric_name
                results = get_metric_statistics(
                    connection=conn,
                    period=period_local,
                    start_time=start_time,
                    end_time=end_time,
                    metric_name=metric_name,
                    namespace=metric['Namespace'],
                    statistics=metric['Statistics'],
                    dimensions=metric['Dimensions'],
                    unit=unit
                )

                if 'NullIsZero' in options and metric_name in options['NullIsZero']:
                    results = value_pad_results(
                        results,
                        start_time,
                        end_time,
                        options['NullIsZero'][metric_name],
                    )

                output_results(results, this_metric, options)
                time.sleep(kwargs.get('interval', 0) / 1000.0)

    # get enhanced monitoring if it is enabled
    if enhanced_monitoring:
        options = get_options(config_options, None, cli_options)
        # convert minutes to seconds
        period_local = options['Period'] * 60
        count_local = options['Count']
        log_group = enhanced_monitoring['LogGroup']
        # determine formatter
        if 'Formatter' in enhanced_monitoring:
            options['Formatter'] = enhanced_monitoring['Formatter']
        # determine if there is a custom formatter for logs in list form
        if 'ListFormatter' in enhanced_monitoring:
            options['ListFormatter'] = enhanced_monitoring['ListFormatter']
        else:
            options['ListFormatter'] = options['Formatter']
        # if you have metrics that are available only every 5 minutes, be sure to request only stats
        # that are likely/sure to be up to date, ie ones ending on the previous
        # period increment.
        # convert date to miliseconds
        end_time = int((datetime.datetime.now() - datetime.timedelta(seconds=int(time.time()) % period_local)).strftime("%s")) * 1000
        start_time = end_time - (period_local * count_local * 1000)
        # connect to endpoint
        logs_conn = boto.logs.connect_to_region(region)
        # get all streams in log group
        log_streams = logs_conn.describe_log_streams(log_group_name=log_group)
        # pluck only stream names out
        streams = map(lambda x: x['logStreamName'], log_streams['logStreams'])
        # retrieve logs for this time period from all streams

        for stream in streams:
            results = get_logs_statistics(
                connection=logs_conn,
                start_from_head=False,
                limit=50,
                start_time=start_time,
                end_time=end_time,
                log_stream_name=stream,
                log_group_name=log_group
            )
            process_log_results(results['events'], options)
            time.sleep(0.5)  # rate limiting


def main(*args, **kwargs):
    options = docopt(__doc__, version=__version__)
    # help: http://boto.readthedocs.org/en/latest/ref/cloudwatch.html#boto.ec2.cloudwatch.CloudWatchConnection.get_metric_statistics
    config_file = options.pop('--config-file')
    period = int(options.pop('--period'))
    count = int(options.pop('-n'))
    verbose = options.pop('-v')

    cli_options = {}
    if period is not None:
        cli_options['Period'] = period
    if count is not None:
        cli_options['Count'] = count
    leadbutt(config_file, cli_options, verbose,
             interval=float(options.pop('-i')),
             max_interval=float(options.pop('-m'))
             )


if __name__ == '__main__':
    main()
