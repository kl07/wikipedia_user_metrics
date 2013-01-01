
"""
    This module contains custom methods to extract time series data.
"""

__author__ = "ryan faulkner"
__date__ = "12/10/2012"
__license__ = "GPL (version 2 or later)"

import datetime
import time
import os
from copy import deepcopy
from dateutil.parser import parse as date_parse
import sys
import logging
import operator
import json

import src.etl.aggregator as agg
import src.metrics.bytes_added as ba
import src.metrics.threshold as th
import src.metrics.revert_rate as rr
import src.etl.data_loader as dl
import src.metrics.user_metric as um
from multiprocessing import Process, Queue

logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
    format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%b-%d %H:%M:%S')

def _get_timeseries(date_start, date_end, interval):
    """ Generates a series of timestamps given a start date, end date, and interval"""

    # Ensure the dates are string representations
    date_start = um.UserMetric._get_timestamp(date_start)
    date_end = um.UserMetric._get_timestamp(date_end)

    # ensure that at least two intervals are included in the time series
    if (date_parse(date_end) - date_parse(date_start)).total_seconds() / 3600 < interval:
        raise TimeSeriesException(message="Time series must contain at least one interval.")

    c = date_parse(date_start) + datetime.timedelta(hours=-interval)
    e = date_parse(date_end)
    while c < e:
        c += datetime.timedelta(hours=interval)
        yield c

def _get_newly_registered_users(date_start, date_end, project):
    """ Produces a set of newly registered users givem """
    sql = """
            select
                log_user
            from %(project)s.logging
            where log_timestamp >= %(date_start)s and log_timestamp <= %(date_end)s and
                log_action = 'create' AND log_type='newusers'
        """ % {
        'project' : project,
        'date_start' : date_start,
        'date_end' : date_end
    }
    return dl.DataLoader().get_elem_from_nested_list(
        dl.Connector(instance='slave').execute_SQL(" ".join(sql.strip().split('\n'))),0)

def threshold_editors(args,interval,log=True,project='enwiki',k=1,n=1,t=1440):
    """ Computes a list of threshold metrics """

    time_series = _get_timeseries(args.date_start, args.date_end, interval)

    ts_s = time_series.next()
    while 1:
        try:
            ts_e = time_series.next()
        except StopIteration:
            break

        if log: print str(datetime.datetime.now()) + ' - Processing time series data for %s...' % str(ts_s)
        user_ids = _get_newly_registered_users(args.date_start, args.date_end,project)

        # Build an iterator across users for a given threshold
        threshold_obj = th.Threshold(date_start=ts_s, date_end=ts_e,n=n,t=t).process(user_ids,
            log_progress=True, num_threads=k)
        total, pos, rate = th.threshold_editors_agg(threshold_obj)

        if log: print " ".join(['timestamp = ', str(ts_s), ', total registrations = ',
                            str(total), ', % prod editors = ',str(rate)])

        yield (ts_s, total, rate) # yields: (timestamp, total registrations, fraction of productive)
        ts_s = ts_e

def reverted(args,interval,log=True,project='enwiki',la=15,lb=15,k=1):

    """ Computes a list of threshold metrics """

    time_series = _get_timeseries(args.date_start, args.date_end, interval)
    ts_s = time_series.next()

    while 1:
        try:
            ts_e = time_series.next()
        except StopIteration:
            break

        if log: print str(datetime.datetime.now()) + ' - Processing time series data for %s...' % str(ts_s)
        user_ids = _get_newly_registered_users(args.date_start, args.date_end,project)

        revert_obj = rr.RevertRate(date_start=ts_s, date_end=ts_e,look_ahead=la,look_back=lb).process(user_ids,
            log_progress=True, num_threads=k)
        total_revs, weighted_rate, total_editors, reverted_editors = rr.reverted_revs_agg(revert_obj)

        if log:
            print " ".join(['timestamp = ', str(ts_s), ', total revisions = ',
                            str(total_revs), ', total revert rate = ',str(weighted_rate),
                            ' total editors = ', str(total_editors), ' reverted editors = ', str(reverted_editors)])

        yield (ts_s, total_revs, weighted_rate, total_editors, reverted_editors)
        ts_s = ts_e

def build_time_series(start, end, interval, metric, aggregator, cohort, **kwargs):
    """
        Builds a timeseries dataset of .

            Parameters:
                - **start**: str or datetime. date + time indicating start of time series
                - **end**: str or datetime. date + time indicating end of time series
                - **interval**: int. integer value in hours that defines the amount of time between data-points
                - **metric**: class object. Metrics class (derived from UserMetric)
                - **aggregator**: method. Aggregator method used to aggregate data for time series data points
                - **cohort**: list(str). list of user IDs
        e.g.

        >>> cohort = ['156171','13234584']
        >>> metric = ba.BytesAdded
        >>> aggregator = agg.list_sum_indices
        >>> field_indices = ba.BytesAdded._data_model_meta['integer_fields']

        >>> build_time_series('20120101000000', '20120112000000', 24, metric, aggregator, cohort,
            num_threads=4, num_threads_metric=2, log=True)

    """

    log = bool(kwargs['log']) if 'log' in kwargs else False

    # Get datetime types, and the number of threads
    start = date_parse(um.UserMetric._get_timestamp(start))
    end = date_parse(um.UserMetric._get_timestamp(end))
    k = kwargs['num_threads'] if 'num_threads' in kwargs else 1

    # Compute window size and ensure that all the conditions necessary to generate a proper time series are met
    num_intervals = int((end - start).total_seconds() / (3600 * interval))
    intervals_per_thread = num_intervals / k

    # Compose the sets of time series lists
    f = lambda t,i:  t + datetime.timedelta(hours = intervals_per_thread * interval * i)
    time_series = [_get_timeseries(f(start, i), f(start, i+1), interval) for i in xrange(k)]
    if f(start, k) <  end: time_series.append(_get_timeseries(f(start, k), end, interval))

    data = list()
    q = Queue()
    processes = list()

    if log: logging.info('Spawning procs, %s - %s, interval = %s, threads = %s ... ' % (
        str(start), str(end), interval, k))
    for i in xrange(len(time_series)):
        p = Process(target=time_series_worker, args=(time_series[i], metric, aggregator, cohort, kwargs, q))
        p.start()
        processes.append(p)

    while 1:
        time.sleep(10) # sleep before checking worker threads

        if log:
            logging.info('Process queue: ' + str(processes))
            logging.info('Data: ' + str(data))

        while not q.empty():
            data.extend(q.get())
        for p in processes:
            if not p.is_alive():
                p.terminate()
                processes.remove(p)

        # exit if all process have finished
        if not len(processes):
            break

    # sort
    return sorted(data, key=operator.itemgetter(0), reverse=False)

def time_series_worker(time_series, metric, aggregator, cohort, kwargs, q):
    """ worker thread which computes time series data for a set of points """
    log = bool(kwargs['log']) if 'log' in kwargs else False

    data = list()
    ts_s = time_series.next()
    new_kwargs = deepcopy(kwargs)

    # re-map some keyword args relating to thread counts
    if 'metric_threads' in new_kwargs:
        d = json.loads(new_kwargs['metric_threads'])
        for key in d: new_kwargs[key] = d[key]
        del new_kwargs['metric_threads']

    while 1:
        try: ts_e = time_series.next()
        except StopIteration: break

        if log: logging.info('Processing thread %s, %s - %s ...' % (os.getpid(), str(ts_s), str(ts_e)))

        # process metrics - ensure that there is some data generated by the request
        metric_obj = metric(date_start=ts_s,date_end=ts_e,**new_kwargs).process(cohort, **new_kwargs)
        r = um.aggregator(aggregator, metric_obj, metric.header(), field_indices)
        data.append([str(ts_s), str(ts_e)] + r.data)

        ts_s = ts_e
    q.put(data) # add the data to the queue

class DataTypeMethods(object):
    DATA_TYPE = {'prod' : threshold_editors, 'revert' : reverted}

class TimeSeriesException(Exception):
    """ Basic exception class for UserMetric types """
    def __init__(self, message="Could not generate time series."):
        Exception.__init__(self, message)

if __name__ == '__main__':

    cohort = ['156171','13234584']
    metric = ba.BytesAdded
    aggregator = agg.list_sum_indices
    field_indices = ba.BytesAdded._data_model_meta['integer_fields']

    print build_time_series('20120101000000', '20120112000000', 24, metric, aggregator, cohort,
        num_threads=4, num_threads_metric=2, log=True)