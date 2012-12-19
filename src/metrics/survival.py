
__author__ = "Ryan Faulkner"
__date__ = "December 6th, 2012"
__license__ = "GPL (version 2 or later)"

import datetime
import user_metric as um
import threshold as th

class Survival(um.UserMetric):
    """
        Boolean measure of the retention of editors. Editors are considered "surviving" if they continue to participate
        after t minutes since an event (often, the user's registration or first edit).

            `https://meta.wikimedia.org/wiki/Research:Metrics/survival(t)`

        As a UserMetric type this class utilizes the process() function attribute to produce an internal list of metrics by
        user handle (typically ID but user names may also be specified). The execution of process() produces a nested list that
        stores in each element:

            * User ID
            * boolean flag to indicate whether the user met the survival criteria

        usage e.g.: ::

            >>> import src.etl.threshold as t
            >>> for r in t.Threshold().process([13234584]).__iter__(): print r
            (13234584L, 1)

    """

    # Structure that defines parameters for Survival class
    _param_types = {
        'init' : {
            'date_start' : ['str|datatime', 'Earliest date a block is measured.'],
            'date_end' : ['str|datatime', 'Latest date a block is measured.'],
            't' : ['int', 'The time in minutes registration after which survival is measured.'],
            },
        'process' : {
            'is_id' : ['bool', 'Are user ids or names being passed.'],
            'log_progress' : ['bool', 'Enable logging for processing.'],
            'num_threads' : ['int', 'Number of worker processes over users.'],
            }
    }

    def __init__(self,
                 date_start='2001-01-01 00:00:00',
                 date_end=datetime.datetime.now(),
                 t=1440,
                 **kwargs):

        """
            - Parameters:
                - **date_start**: string or datetime.datetime. start date of edit interval
                - **date_end**: string or datetime.datetime. end date of edit interval
        """
        self._start_ts_ = self._get_timestamp(date_start)
        self._end_ts_ = self._get_timestamp(date_end)
        self._t_ = t
        um.UserMetric.__init__(self, **kwargs)
        self.append_params(um.UserMetric)   # add params from base class

    @staticmethod
    def header():
        return ['user_id', 'is_alive']

    def process(self, user_handle, is_id=True, **kwargs):

        """
            Wraps the functionality of UserMetric::Threshold by setting the `survival` flag in process().

            - Parameters:
                - **user_handle** - String or Integer (optionally lists).  Value or list of values representing user handle(s).
                - **is_id** - Boolean.  Flag indicating whether user_handle stores user names or user ids

        """

        k = kwargs['num_threads'] if 'num_threads' in kwargs else 0
        log_progress = bool(kwargs['log_progress']) if 'log_progress' in kwargs else False

        return th.Threshold(
            date_start=self._start_ts_,
            date_end=self._end_ts_,
            n=0,
            t=self._t_).process(user_handle, survival=True, num_threads=k, log_progress=log_progress)

# testing
if __name__ == "__main__":
    # did these users survive after a day?
    for r in Survival().process([13234584, 156171], num_threads=2, log_progress=True).__iter__(): print r