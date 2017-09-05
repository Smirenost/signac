# Copyright (c) 2017 The Regents of the University of Michigan
# All rights reserved.
# This software is licensed under the BSD 3-Clause License.
import os
import re
import shutil
import filecmp
import logging
from collections.abc import Mapping
from collections import OrderedDict
from contextlib import contextmanager

from .errors import DestinationExistsError
from .errors import MergeConflict
from .errors import MergeSchemaConflict
from .utility import query_yes_no


LEVEL_MORE = logging.INFO - 5

logger = logging.getLogger(__name__)
logging.addLevelName(LEVEL_MORE, 'MORE')
logging.MORE = LEVEL_MORE


def log_more(msg, *args, **kwargs):
    logger.log(LEVEL_MORE, msg, *args, **kwargs)


logger.more = log_more


__all__ = [
    'merge_jobs',
    'merge_projects',
    'MERGE_STRATEGIES',
    'ask',
    'ours',
    'theirs',
    'last_modified',
]


# Definition of default merge strategies
def theirs(fn_src, fn_dst):
    "Merge strategy: Always merge files on conflict."
    return True


def ours(fn_src, fn_dst):
    "Merge strategy: Never merge files on conflict."
    return False


def ask(fn_src, fn_dst):
    "Merge strategy: Ask whether a file should be merged interactively."
    return query_yes_no(
        "Overwrite file '{}' with '{}'?".format(fn_src, fn_dst),
        'no')


def last_modified(fn_src, fn_dst):
    "Merge strategy: Merge a file based on its modification time stamp."
    return os.path.getmtime(fn_src) > os.path.getmtime(fn_dst)


MERGE_STRATEGIES = OrderedDict([
    ('ask', ask),
    ('ours', ours),
    ('theirs', theirs),
    ('last_modified', last_modified),
])
"A ordered dictionary of default merge strategies."


# Modification Proxy

class _DataModifyProxy(object):
    """This proxy used for data modification.

    By performing all data modification operations on the proxy,
    we can ensure consistent logging and dry run behavior.

    :param dry_run:
        Do not actually perform any data modification operation, but
        still log the action.
    :type dry_run:
        bool
    """

    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def set_value(self, doc, key, value):
        logger.more("Set '{}'='{}'.".format(key, value))
        if not self.dry_run:
            doc[key] = value

    def copy(self, src, dst):
        logger.more("Copy file '{}' -> '{}'.".format(os.path.relpath(src), os.path.relpath(dst)))
        if not self.dry_run:
            shutil.copy(src, dst)

    def copytree(self, src, dst):
        logger.more("Copy tree '{}' -> '{}'.".format(os.path.relpath(src), os.path.relpath(dst)))
        if not self.dry_run:
            shutil.copytree(src, dst)

    def remove(self, path):
        logger.more("Remove path '{}'.".format(os.path.relpath(path)))
        if not self.dry_run:
            os.remove(path)

    @contextmanager
    def create_backup(self, path):
        logger.debug("Create backup of '{}'.".format(os.path.relpath(path)))
        path_backup = path + '~'
        if os.path.isfile(path_backup):
            raise RuntimeError(
                "Failed to create backup, file already exists: '{}'.".format(
                    os.path.relpath(path_backup)))
        try:
            if not self.dry_run:
                shutil.copy(path, path_backup)
            yield path_backup
        finally:
            logger.debug("Remove backup of '{}'.".format(os.path.relpath(path)))
            if not self.dry_run:
                try:
                    os.remove(path_backup)
                except IOError as error:
                    logger.error(error)


# Merge algorithms

def _merge_dicts(src, dst, strategy, proxy):
    if src == dst:
        return set()
    skipped_keys = set()
    for key, value in src.items():
        if key in dst:
            if dst[key] == value:
                continue
            elif strategy is None or not strategy(key):
                skipped_keys.add(key)
                continue
            elif isinstance(value, Mapping):
                try:
                    skipped_keys.update(_merge_dicts(src[key], dst[key], strategy, proxy))
                    continue
                except KeyError:
                    pass

        proxy.set_value(dst, key, value)
    return skipped_keys


def _merge_json_dicts(src, dst, strategy, proxy):
    if dst._filename is None or not os.path.isfile(dst._filename):
        return _merge_dicts(src, dst, strategy, proxy)
    else:
        with proxy.create_backup(dst._filename) as fn_backup:
            try:
                return _merge_dicts(src, dst, strategy, proxy)
            except Exception:
                # Try to restore backup
                logger.warning("Error during json dict merge, restoring backup...")
                proxy.copy(fn_backup, dst._filename)
                raise


def _merge_dirs(src, dst, exclude, strategy, proxy):
    "Merge two directories."
    diff = filecmp.dircmp(src, dst)
    for fn in diff.left_only:
        if exclude and any([re.match(p, fn) for p in exclude]):
            logger.debug("File '{}' is skipped (excluded).".format(fn))
            continue
        fn_src = os.path.join(src, fn)
        fn_dst = os.path.join(dst, fn)
        if os.path.isfile(fn_src):
            proxy.copy(fn_src, fn_dst)
        else:
            proxy.copytree(os.path.join(src, fn), os.path.join(dst, fn))
    for fn in diff.diff_files:
        if exclude and any([re.match(p, fn) for p in exclude]):
            logger.debug("File '{}' is skipped (excluded).".format(fn))
            continue
        if strategy is None:
            raise MergeConflict(fn)
        else:
            fn_src = os.path.join(src, fn)
            fn_dst = os.path.join(dst, fn)
            if strategy(fn_src, fn_dst):
                proxy.copy(fn_src, fn_dst)
            else:
                logger.debug("Skip file '{}'.".format(fn))
    for subdir in diff.subdirs:
        _merge_dirs(os.path.join(src, subdir), os.path.join(dst, subdir), exclude, strategy, proxy)


def merge_jobs(src_job, dst_job, exclude=None, strategy=None, doc_strategy=None, dry_run=False):
    "Merge two jobs."
    if exclude is None:
        exclude = []
    elif not isinstance(exclude, list):
        exclude = [exclude]
    if type(dry_run) == _DataModifyProxy:
        proxy = dry_run
    else:
        proxy = _DataModifyProxy(dry_run=bool(dry_run))

    if proxy.dry_run:
        logger.debug("Merging job '{}' (dry run)...".format(src_job))
    else:
        logger.debug("Merging job '{}'...".format(src_job))
    assert type(src_job) == type(dst_job)
    assert src_job.get_id() == dst_job.get_id()
    assert src_job.FN_MANIFEST == dst_job.FN_MANIFEST
    assert src_job.FN_DOCUMENT == dst_job.FN_DOCUMENT
    exclude.extend((src_job.FN_MANIFEST, src_job.FN_DOCUMENT))
    _merge_dirs(src_job.workspace(), dst_job.workspace(), exclude, strategy, proxy)
    return _merge_json_dicts(src_job.doc, dst_job.doc, doc_strategy, proxy)


def merge_projects(source, destination, exclude=None, strategy=None, doc_strategy=None,
                   selection=None, check_schema=True, dry_run=False):
    """Merge the source project into the destination project.

    Try to clone all jobs from the source to the destination.
    If the destination job already exist, try to merge the job using the
    optionally specified strategy.
    """
    if source == destination:
        raise ValueError("Source and destination can't be the same!")

    # Setup data modification proxy
    proxy = _DataModifyProxy(dry_run=dry_run)

    # Perform a schema check in an attempt to avoid bad merge operations.
    if check_schema:
        schema_src = source.detect_schema()
        schema_dst = destination.detect_schema()
        if schema_dst and schema_src != schema_dst:
            if schema_src.difference(schema_dst) or schema_dst.difference(schema_src):
                raise MergeSchemaConflict(schema_src, schema_dst)

    if selection is not None:  # The selection argument may be a jobs or job ids sequence.
        selection = {str(j) for j in selection}

    # Provide some information about this merge process.
    if selection:
        logger.info("Merging selection ({}) of project '{}' into '{}'.".format(
            len(selection), source, destination))
    else:
        logger.info("Merging project '{}' into '{}'.".format(source, destination))
    logger.more("'{}' -> '{}'".format(source.root_directory(), destination.root_directory()))
    if dry_run:
        logger.info("Performing dry run!")
    if exclude is not None:
        logger.more("Exclude pattern: '{}'".format(exclude))
    logger.more("Merge strategy: '{}'".format(strategy))

    # Keep track of all document keys skipped during merging.
    skipped_keys = set()

    # Merge the Project document.
    skipped_keys.update(_merge_json_dicts(
        source.document, destination.document, doc_strategy, proxy))

    # Merge jobs from source to destination.
    cloned, merged = 0, 0
    for src_job in source:
        if selection is not None and src_job.get_id() not in selection:
            logger.more("{} not in selection.".format(src_job))
            continue
        try:
            destination.clone(src_job)
            cloned += 1
            logger.more("Cloned job '{}'.".format(src_job))
        except DestinationExistsError as e:
            dst_job = destination.open_job(id=src_job.get_id())
            skipped_keys.update(merge_jobs(src_job, dst_job, exclude,
                                           strategy, doc_strategy, proxy))
            merged += 1
            logger.more("Merged job '{}'.".format(src_job))
    logger.info("Cloned {} and merged {} job(s).".format(cloned, merged))

    # Provide some information about skipped document keys.
    if skipped_keys:
        logger.info("Skipped {} document key(s).".format(len(skipped_keys)))
        logger.more("Skipped key(s): {}".format(', '.join(skipped_keys)))
    return skipped_keys
