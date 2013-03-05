
"""
    The engine for the metrics API which stores definitions an backend API
    operations.  This module defines the communication between API requests
    and UserMetric objects, how and where request responses are stored, and
    how cohorts are parsed from API request URLs.

    Data Storage and Retrieval
    ~~~~~~~~~~~~~~~~~~~~~~~~~~

    This portion of the API engine is concerned with extracting and storing
    data at runtime to service API requests.  This includes all of the
    interaction with the ``usertags`` and ``usertags_meta`` SQL tables that
    define user cohorts.  The following methods are involved with extracting
    this data::

        get_users(cohort_expr)
        get_cohort_id(utm_name)
        get_cohort_refresh_datetime(utm_id)

    The other portion of data storage and retrieval is concerned with providing
    functionality that enables responses to be cached.  Request responses are
    currently cached in the ``pkl_data`` OrderedDict_ defined in the run
    module.  This object stores responses in a nested fashion using URL request
    variables and their corresponding values.  For example, the url
    ``http://metrics-api.wikimedia.org/cohorts/e3_ob2b/revert_rate?t=10000``
    maps to::

        pkl_data['cohort_expr <==> e3_ob2b']['metric <==> revert_rate']
        ['date_start <==> xx']['date_start <==> yy']['t <==> 10000']

    The list of key values for a given request is referred to as it's "key
    signature".  The order of parameters is perserved.

    The ``get_data`` method requires a reference to ``pkl_data``. Given this
    reference and a RequestMeta object the method attempts to find an entry
    for the request if one exists.  The ``set_data`` method does much the
    same operation but performs storage into the hash reference passed.
    The method ``get_url_from_keys`` builds URLs from nested hash references
    using the key list and ``build_key_tree`` recursively builds a tree
    representation of all of the key paths in the hash reference.

    .. _OrderedDict: http://docs.python.org/2/library/collections.html

    Cohort request parsing
    ~~~~~~~~~~~~~~~~~~~~~~

    This set of methods allows boolean expressions of cohort IDs to be
    synthesized and interpreted in the portion of the URL path that is
    bound to the user cohort name.  This set of methods, invoked at the top
    level via ``parse_cohorts`` takes an expression of the form::

        http://metrics-api.wikimedia.org/cohorts/1&2~3~4/bytes_added

    The portion of the path ``1&2~3~4``, resolves to the boolean expression
    "1 AND 2 OR 3 OR 4".  The cohorts that correspond to the numeric ID values
    in ``usertags_meta`` are resolved to sets of user ID longs which are then
    operated on with union and intersect operations to yield a custom user
    list.  The power of this functionality lies in that it allows subsets of
    users to be selected based on prior conditions that includes them in a
    given cohort.

    Method Definitions
    ~~~~~~~~~~~~~~~~~~
"""

__author__ = "ryan faulkner"
__email__ = "rfaulkner@wikimedia.org"
__date__ = "january 11 2012"
__license__ = "GPL (version 2 or later)"

from flask import escape, redirect, url_for
from datetime import datetime
from re import search
from collections import OrderedDict
import user_metrics.etl.data_loader as dl
from user_metrics.config import logging
from user_metrics.api.engine.request import REQUEST_META_QUERY_STR, \
    REQUEST_META_BASE
from user_metrics.api import MetricsAPIError


#
# Define constants for request_manager module
# ===========================================

# Define Process status types
JOB_STATUS_TYPES = ['pending', 'running', 'success', 'failure']
JOB_STATUS = eval('enum("' + '","'.join(JOB_STATUS_TYPES) +
                  '", **' + str({t[0]: t[1] for t in zip(JOB_STATUS_TYPES,
    JOB_STATUS_TYPES)}) + ')')

# Number of maximum concurrently running jobs
MAX_CONCURRENT_JOBS = 1

# Time to block on waiting for a new request to appear in the queue
QUEUE_WAIT = 1



#
# Define remaining constants
# ==========================
# @TODO break these out into separate modules

# Regex that matches a MediaWiki user ID
MW_UID_REGEX = r'^[0-9]{5}[0-9]*$'
MW_UNAME_REGEX = r'[a-zA-Z_\.\+ ]'


# This is used to separate key meta and key strings for hash table data
# e.g. "metric <==> blocks"
HASH_KEY_DELIMETER = " <==> "

# Datetime string format to be used throughout the API
DATETIME_STR_FORMAT = "%Y%m%d%H%M%S"

# The default value for non-assigned and valid values in the query string
DEFAULT_QUERY_VAL = 'present'



#
# Data retrieval and storage methods
#
# ==================================


def get_users(cohort_expr):
    """ get users from cohort """

    if search(COHORT_REGEX, cohort_expr):
        logging.info(__name__ + '::Processing cohort by expression.')
        users = [user for user in parse_cohorts(cohort_expr)]
    else:
        logging.info(__name__ + '::Processing cohort by tag name.')
        conn = dl.Connector(instance='slave')
        try:
            conn._cur_.execute('select utm_id from usertags_meta '
                               'WHERE utm_name = "%s"' % str(cohort_expr))
            res = conn._cur_.fetchone()[0]
            conn._cur_.execute('select ut_user from usertags '
                               'WHERE ut_tag = "%s"' % res)
        except IndexError:
            redirect(url_for('cohorts'))
        users = [r[0] for r in conn._cur_]
        del conn
    return users


def get_cohort_id(utm_name):
    """ Pull cohort ids from cohort handles """
    conn = dl.Connector(instance='slave')
    conn._cur_.execute('SELECT utm_id FROM usertags_meta '
                       'WHERE utm_name = "%s"' % str(escape(utm_name)))

    utm_id = None
    try:
        utm_id = conn._cur_.fetchone()[0]
    except ValueError:
        pass

    # Ensure the field was retrieved
    if not utm_id:
        logging.error(__name__ + '::Missing utm_id for cohort %s.' %
                                 str(utm_name))
        utm_id = -1

    del conn
    return utm_id


def get_cohort_refresh_datetime(utm_id):
    """
        Get the latest refresh datetime of a cohort.  Returns current time
        formatted as a string if the field is not found.
    """
    conn = dl.Connector(instance='slave')
    conn._cur_.execute('SELECT utm_touched FROM usertags_meta '
                       'WHERE utm_id = %s' % str(escape(utm_id)))

    utm_touched = None
    try:
        utm_touched = conn._cur_.fetchone()[0]
    except ValueError:
        pass

    # Ensure the field was retrieved
    if not utm_touched:
        logging.error(__name__ + '::Missing utm_touched for cohort %s.' %
                                 str(utm_id))
        utm_touched = datetime.now()

    del conn
    return utm_touched.strftime(DATETIME_STR_FORMAT)


def get_data(request_meta, hash_table_ref):
    """ Extract data from the global hash given a request object """

    # Traverse the hash key structure to find data
    # @TODO rather than iterate through REQUEST_META_BASE &
    #   REQUEST_META_QUERY_STR look only at existing attributes

    logging.debug(__name__ + "::Attempting to pull data for request {0}".
    format(str(request_meta)))
    for key_name in REQUEST_META_BASE + REQUEST_META_QUERY_STR:
        if hasattr(request_meta, key_name) and getattr(request_meta, key_name):
            key = getattr(request_meta, key_name)
        else:
            continue

        full_key = key_name + HASH_KEY_DELIMETER + key
        if hasattr(hash_table_ref, 'has_key') and full_key in hash_table_ref:
            hash_table_ref = hash_table_ref[full_key]
        else:
            return None

    # Ensure that an interface that does not rely on keyed values is returned
    # all data must be in interfaces resembling lists
    if not hasattr(hash_table_ref, '__iter__'):
        return hash_table_ref
    else:
        return None


def set_data(request_meta, data, hash_table_ref):
    """
        Given request meta-data and a dataset create a key path in the global
        hash to store the data
    """

    key_sig = list()

    # Build the key signature -- These keys must exist
    for key_name in REQUEST_META_BASE:
        key = getattr(request_meta, key_name)
        if key:
            key_sig.append(key_name + HASH_KEY_DELIMETER + key)
        else:
            logging.error(__name__ + '::Request must include %s. '
                                     'Cannot set data %s.' %
                                     (key_name, str(request_meta)))
            return

    # These keys may optionally exist
    for key_name in REQUEST_META_QUERY_STR:
        if hasattr(request_meta, key_name):
            key = getattr(request_meta, key_name)
            if key:
                key_sig.append(key_name + HASH_KEY_DELIMETER + key)

    logging.debug(__name__ + "::Adding data to hash @ key signature = {0}".
    format(str(key_sig)))
    # For each key in the key signature add a nested key to the hash
    last_item = key_sig[len(key_sig) - 1]
    for key in key_sig:
        if key != last_item:
            if not (hasattr(hash_table_ref, 'has_key') and
                    key in hash_table_ref and
                    hasattr(hash_table_ref[key], 'has_key')):
                hash_table_ref[key] = OrderedDict()

            hash_table_ref = hash_table_ref[key]
        else:
            hash_table_ref[key] = data


def get_url_from_keys(keys, path_root):
    """ Compose a url from a set of keys """
    query_str = ''
    for key in keys:
        parts = key.split(HASH_KEY_DELIMETER)
        if parts[0] in REQUEST_META_BASE:
            path_root += '/' + parts[1]
        elif parts[0] in REQUEST_META_QUERY_STR:
            query_str += parts[0] + '=' + parts[1] + '&'

    if not path_root:
        raise MetricsAPIError()
    if query_str:
        url = path_root + '?' + query_str[:-1]
    else:
        url = path_root
    return url


def build_key_tree(nested_dict):
    """ Builds a tree of key values from a nested dict. """
    if hasattr(nested_dict, 'keys'):
        for key in nested_dict.keys():
            yield (key, build_key_tree(nested_dict[key]))
    else:
        yield None

#
# Cohort parsing methods
#
# ======================

# This regex must be matched to parse cohorts
COHORT_REGEX = r'^([0-9]+[&~])*[0-9]+$'

COHORT_OP_AND = '&'
COHORT_OP_OR = '~'
# COHORT_OP_NOT = '^'


def parse_cohorts(expression):
    """
        Defines and parses boolean expressions of cohorts and returns a list
        of user ids corresponding to the expression argument.

            Parameters:
                - **expression**: str. Boolean expression built of
                    cohort labels.

            Return:
                - List(str).  user ids corresponding to cohort expression.
    """

    # match expression
    if not search(COHORT_REGEX, expression):
        raise MetricsAPIError()

    # parse expression
    return parse(expression)


def parse(expression):
    """ Top level parsing. Splits expression by OR then sub-expressions by
        AND. returns a generator of ids included in the evaluated expression
    """
    user_ids_seen = set()
    for sub_exp_1 in expression.split(COHORT_OP_OR):
        for user_id in intersect_ids(sub_exp_1.split(COHORT_OP_AND)):
            if not user_ids_seen.__contains__(user_id):
                user_ids_seen.add(user_id)
                yield user_id


def get_cohort_ids(conn, cohort_id):
    """ Returns string valued ids corresponding to a cohort """
    sql = """
        SELECT ut_user
        FROM staging.usertags
        WHERE ut_tag = %(id)s
    """ % {
        'id': str(cohort_id)
    }
    conn._cur_.execute(sql)
    for row in conn._cur_:
        yield str(row[0])


def intersect_ids(cohort_id_list):

    conn = dl.Connector(instance='slave')

    user_ids = dict()
    # only a single cohort id in the expression - return all users of this
    # cohort
    if len(cohort_id_list) == 1:
        for id in get_cohort_ids(conn, cohort_id_list[0]):
            yield id
    else:
        for cid in cohort_id_list:
            for id in get_cohort_ids(conn, cid):
                if id in user_ids:
                    user_ids[id] += 1
                else:
                    user_ids[id] = 1
                    # Executes only in the case that there was more than one cohort
                    # id in the expression
        for key in user_ids:
            if user_ids[key] > 1:
                yield key
    del conn
