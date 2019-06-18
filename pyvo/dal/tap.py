# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
A module for accessing remote source and observation catalogs
"""
from __future__ import (
    absolute_import, division, print_function, unicode_literals)

from functools import partial
from datetime import datetime
from time import sleep
from distutils.version import LooseVersion

import requests

from astropy.io.votable import parse as votableparse

from .query import (
    DALResults, DALQuery, DALService, Record, UploadList,
    DALServiceError, DALQueryError)
from .vosi import AvailabilityMixin, CapabilityMixin, VOSITables
from .adhoc import DatalinkResultsMixin, DatalinkRecordMixin, SodaRecordMixin

from ..io import vosi, uws
from ..io.vosi import tapregext as tr

from ..utils.formatting import para_format_desc
from ..utils.http import session

__all__ = [
    "search", "escape", "TAPService", "TAPQuery", "AsyncTAPJob", "TAPResults"]


def escape(term):
    """
    escapes a term for use in ADQL
    """
    return str(term).replace("'", "''")


def search(url, query, language="ADQL", maxrec=None, uploads=None, **keywords):
    """
    submit a Table Access query that returns rows matching the criteria given.

    Parameters
    ----------
    url : str
        the base URL of the query service.
    query : str, dict
        The query string / parameters
    language : str
        specifies the query language, default ADQL.
        useful for services which allow to use the backend query language.
    maxrec : int
        the maximum records to return. defaults to the service default
    uploads : dict
        a mapping from table names to file like objects containing a votable

    Returns
    -------
    TAPResults
        a container holding a table of matching catalog records

    Raises
    ------
    DALServiceError
        for errors connecting to or
        communicating with the service.
    DALQueryError
        if the service responds with
        an error, including a query syntax error.
    """
    service = TAPService(url)
    return service.search(query, language, maxrec, uploads, **keywords)


class TAPService(DALService, AvailabilityMixin, CapabilityMixin):
    """
    a representation of a Table Access Protocol service
    """

    _tables = None

    def __init__(self, baseurl):
        """
        instantiate a Tablee Access Protocol service

        Parameters
        ----------
        baseurl :  str
           the base URL that should be used for forming queries to the service.
        """
        super().__init__(baseurl)

    @property
    def tables(self):
        """
        returns tables as a dict-like object
        """
        if self._tables is None:
            tables_url = '{}/tables'.format(self.baseurl)

            response = session.get(tables_url, stream=True)

            try:
                response.raise_for_status()
            except requests.RequestException as ex:
                raise DALServiceError.from_except(ex, tables_url)

            # requests doesn't decode the content by default
            response.raw.read = partial(response.raw.read, decode_content=True)

            self._tables = VOSITables(
                vosi.parse_tables(response.raw.read), tables_url)
        return self._tables

    @property
    def maxrec(self):
        """
        the default output limit.

        Raises
        ------
        DALServiceError
            if the property is not exposed by the service
        """
        try:
            for capa in self.capabilities:
                if isinstance(capa, tr.TableAccess):
                    return capa.outputlimit.default.content
        except AttributeError:
            pass
        raise DALServiceError("Default limit not exposed by the service")

    @property
    def hardlimit(self):
        """
        the hard output limit.

        Raises
        ------
        DALServiceError
            if the property is not exposed by the service
        """
        try:
            for capa in self.capabilities:
                if isinstance(capa, tr.TableAccess):
                    return capa.outputlimit.hard.content
        except AttributeError:
            pass
        raise DALServiceError("Hard limit not exposed by the service")

    @property
    def upload_methods(self):
        """
        a list of upload methods in form of
        :py:class:`~pyvo.io.vosi.tapregext.UploadMethod` objects
        """
        upload_methods = []
        for capa in self.capabilities:
            if isinstance(capa, tr.TableAccess):
                upload_methods += capa.uploadmethods
        return upload_methods

    def run_sync(
            self, query, language="ADQL", maxrec=None, uploads=None,
            **keywords):
        """
        runs sync query and returns its result

        Parameters
        ----------
        query : str
            The query
        language : str
            specifies the query language, default ADQL.
            useful for services which allow to use the backend query language.
        maxrec : int
            the maximum records to return. defaults to the service default
        uploads : dict
            a mapping from table names to objects containing a votable

        Returns
        -------
        TAPResults
            the query result

        See Also
        --------
        TAPResults
        """
        return self.create_query(
            query, language=language, maxrec=maxrec, uploads=uploads,
            **keywords).execute()

    # alias for service discovery
    search = run_sync

    def run_async(
            self, query, language="ADQL", maxrec=None, uploads=None,
            **keywords):
        """
        runs async query and returns its result

        Parameters
        ----------
        query : str, dict
            the query string / parameters
        language : str
            specifies the query language, default ADQL.
            useful for services which allow to use the backend query language.
        maxrec : int
            the maximum records to return. defaults to the service default
        uploads : dict
            a mapping from table names to objects containing a votable

        Returns
        -------
        TAPResult
            the query instance

        Raises
        ------
        DALServiceError
           for errors connecting to or communicating with the service
        DALQueryError
           for errors either in the input query syntax or
           other user errors detected by the service
        DALFormatError
           for errors parsing the VOTable response

        See Also
        --------
        AsyncTAPJob
        """
        job = AsyncTAPJob.create(
            self.baseurl, query, language, maxrec, uploads, **keywords)
        job = job.run().wait()
        job.raise_if_error()
        result = job.fetch_result()
        job.delete()

        return result

    def submit_job(
            self, query, language="ADQL", maxrec=None, uploads=None,
            **keywords):
        """
        submit a async query without starting it and returns a AsyncTAPJob
        object

        Parameters
        ----------
        query : str
            the query string / parameters
        language : str
            specifies the query language, default ADQL.
            useful for services which allow to use the backend query language.
        maxrec : int
            the maximum records to return. defaults to the service default
        uploads : dict
            a mapping from table names to objects containing a votable

        Returns
        -------
        AsyncTAPJob
            the query instance

        See Also
        --------
        AsyncTAPJob
        """
        return AsyncTAPJob.create(
            self.baseurl, query, language, maxrec, uploads, **keywords)

    def create_query(
            self, query=None, mode="sync", language="ADQL", maxrec=None,
            uploads=None, **keywords):
        """
        create a query object that constraints can be added to and then
        executed.  The input arguments will initialize the query with the
        given values.

        Parameters
        ----------
        baseurl : str
            the base URL for the TAP service
        query : str
            the query string / parameters
        mode : str
            the query mode (sync | async). default "sync"
        language : str
            specifies the query language, default ADQL.
            useful for services which allow to use the backend query language.
        maxrec : int
            specifies the maximum records to return.
            defaults to the service default.
        uploads : dict
            a mapping from table names to objects containing a votable.
        """
        return TAPQuery(
            self.baseurl, query, mode, language, maxrec, uploads, **keywords)

    def describe(self, width=None):
        """
        Print a summary description of this service.

        This includes the interface capabilities, and the content description
        if it doesn't contains multiple data collections (in other words, it is
        not a TAP service).
        """
        if len(self.tables) == 1:
            description = next(self.tables.values()).description

            if width:
                description = para_format_desc(description, width)

            print(description)
            print()

        capabilities = filter(
            lambda x: not str(x.standardid).startswith(
                'ivo://ivoa.net/std/VOSI'),
            self.capabilities
        )

        for cap in capabilities:
            cap.describe()
            print()


class AsyncTAPJob:
    """
    This class represents a UWS TAP Job.
    """

    _job = {}

    @classmethod
    def create(
            cls, baseurl, query, language="ADQL", maxrec=None, uploads=None,
            **keywords):
        """
        creates a async tap job on the server under `baseurl`

        Parameters
        ----------
        baseurl : str
            the TAP baseurl
        query : str
            the query string
        language : str
            specifies the query language, default ADQL.
            useful for services which allow to use the backend query language.
        maxrec : int
            the maximum records to return. defaults to the service default
        uploads : dict
            a mapping from table names to objects containing a votable
        """
        query = TAPQuery(
            baseurl, query, mode="async", language=language, maxrec=maxrec,
            uploads=uploads, **keywords)
        response = query.submit()
        job = cls(response.url)
        return job

    def __init__(self, url):
        """
        initialize the job object with the given url and fetch remote values

        Parameters
        ----------
        url : str
            the job url
        """
        self._url = url
        self._update()

    def __enter__(self):
        """
        Enters the context
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Exits the context. The job is silently deleted.
        """
        try:
            self.delete()
        except Exception:
            pass

    def _update(self, wait_for_statechange=False, timeout=10.):
        """
        updates local job infos with remote values
        """
        try:
            if wait_for_statechange:
                response = session.get(
                    self.url, stream=True, timeout=timeout, params={
                        "WAIT": "-1"
                    }
                )
            else:
                response = session.get(self.url, stream=True, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        # requests doesn't decode the content by default
        response.raw.read = partial(response.raw.read, decode_content=True)

        self._job = uws.parse_job(response.raw.read)

    @property
    def job(self):
        """
        all up-to-date uws job infos as dictionary
        """
        # keep it up to date
        self._update()
        return self._job

    @property
    def url(self):
        """
        the job url
        """
        return self._url

    @property
    def job_id(self):
        """
        the job id
        """
        return self._job.jobid

    @property
    def phase(self):
        """
        the current query phase
        """
        self._update()
        return self._job.phase

    @property
    def execution_duration(self):
        """
        maximum execution duration as ~`astropy.time.TimeDelta`
        """
        self._update()
        return self._job.executionduration

    @execution_duration.setter
    def execution_duration(self, value):
        try:
            response = session.post(
                "{}/executionduration".format(self.url),
                data={"EXECUTIONDURATION": str(value)})
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        self._update()

    @property
    def destruction(self):
        """
        datetime after which the job results are deleted automatically.
        read-write
        """
        self._update()
        return self._job.destruction

    @destruction.setter
    def destruction(self, value):
        """
        datetime after which the job results are deleted automatically.
        read-write

        Parameters
        ----------
        value : datetime
            datetime after which the job results are deleted automatically
        """
        try:
            # is string? easier to ask for forgiveness
            value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

        try:
            response = session.post(
                "{}/destruction".format(self.url),
                data={"DESTRUCTION": value.strftime("%Y-%m-%dT%H:%M:%SZ")})
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        self._update()

    @property
    def quote(self):
        """
        estimated runtime
        """
        self._update()
        return self._job.quote

    @property
    def owner(self):
        """
        job owner (if applicable)
        """
        self._update()
        return self._job.ownerid

    @property
    def query(self):
        """
        the job query
        """
        self._update()
        for parameter in self._job.parameters:
            if parameter.id_ == 'query':
                return parameter.content
        return ''

    @query.setter
    def query(self, query):
        try:
            response = session.post(
                '{}/parameters'.format(self.url),
                data={"QUERY": query})
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        self._update()

    def upload(self, **kwargs):
        """
        upload a table to the job. the job must not been started.
        """
        uploads = UploadList.fromdict(kwargs)
        files = {
            upload.name: upload.fileobj()
            for upload in uploads
            if upload.is_inline
        }

        try:
            response = session.post(
                '{}/parameters'.format(self.url),
                data={'UPLOAD': uploads.param()},
                files=files
            )
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        self._update()

    @property
    def results(self):
        """
        The job results if exists
        """
        return self._job.results

    @property
    def result(self):
        """
        The job result if exists
        """
        try:
            for r in self._job.results:
                if r.id_ == 'result':
                    return r

            return self._job.results[0]
        except IndexError:
            return None

    @property
    def result_uris(self):
        """
        a list of the last result uri's
        """
        return [result.href for result in self._job.results]

    @property
    def result_uri(self):
        """
        the uri of the result
        """
        try:
            return self.result.href
        except IndexError:
            return None

    @property
    def uws_version(self):
        self._update()
        return self._job.version

    def run(self):
        """
        starts the job / change phase to RUN
        """
        try:
            response = session.post(
                '{}/phase'.format(self.url), data={"PHASE": "RUN"})
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        return self

    def abort(self):
        """
        aborts the job / change phase to ABORT
        """
        try:
            response = session.post(
                '{}/phase'.format(self.url), data={"PHASE": "ABORT"})
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        return self

    def wait(self, phases=None, timeout=600.):
        """
        waits for the job to reach the given phases.

        Parameters
        ----------
        phases : list
            phases to wait for

        Raises
        ------
        DALServiceError
            if the job is in a state that won't lead to an result
        """
        if not phases:
            phases = {"COMPLETED", "ABORTED", "ERROR"}

        interval = 1.0
        increment = 1.2

        active_phases = {
            "QUEUED", "EXECUTING", "RUN", "COMPLETED", "ERROR", "UNKNOWN"}

        while True:
            self._update(wait_for_statechange=True, timeout=timeout)
            # use the cached value
            cur_phase = self._job.phase

            if cur_phase not in active_phases:
                raise DALServiceError(
                    "Cannot wait for job completion. Job is not active!")

            if cur_phase in phases:
                break

            # fallback for uws 1.0
            if LooseVersion(self._job.version) < LooseVersion("1.1"):
                sleep(interval)
                interval = min(120, interval * increment)

        return self

    def delete(self):
        """
        deletes the job. this object will become invalid.
        """
        try:
            response = session.post(self.url, data={"ACTION": "DELETE"})
            response.raise_for_status()
        except requests.RequestException as ex:
            raise DALServiceError.from_except(ex, self.url)

        self._url = None

    def raise_if_error(self):
        """
        raise a exception if theres an error

        Raises
        ------
        DALQueryError
            if theres an error
        """
        if self.phase in {"ERROR", "ABORTED"}:
            raise DALQueryError("Query Error", self.phase, self.url)

    def fetch_result(self):
        """
        returns the result votable if query is finished
        """
        try:
            response = session.get(self.result_uri, stream=True)
            response.raise_for_status()
        except requests.RequestException as ex:
            self._update()
            # we propably got a 404 because query error. raise with error msg
            self.raise_if_error()
            raise DALServiceError.from_except(ex, self.url)

        response.raw.read = partial(
            response.raw.read, decode_content=True)
        return TAPResults(votableparse(response.raw.read), url=self.result_uri)


class TAPQuery(DALQuery):
    """
    a class for preparing an query to an TAP service.  Query constraints
    are added via its service type-specific methods.  The various execute()
    functions will submit the query and return the results.

    The base URL for the query, which controls where the query will be sent
    when one of the execute functions is called, is typically set at
    construction time; however, it can be updated later via the
    :py:attr:`~pyvo.dal.query.DALQuery.baseurl` to send a configured
    query to another service.

    In addition to the search constraint attributes described below, search
    parameters can be set generically by name via dict semantics.

    The typical function for submitting the query is ``execute()``; however,
    alternate execute functions provide the response in different forms,
    allowing the caller to take greater control of the result processing.
    """

    def __init__(
            self, baseurl, query, mode="sync", language="ADQL", maxrec=None,
            uploads=None, **keywords):
        """
        initialize the query object with the given parameters

        Parameters
        ----------
        baseurl : str
            the TAP baseurl
        query : str
            the query string
        mode : str
            the query mode (sync | async). default "sync"
        language : str
            the query language. defaults to ADQL
        maxrec : int
            the amount of records to fetch
        uploads : dict
            Files to upload. Uses table name as key and table content as value.
        """
        baseurl = baseurl.rstrip("?")

        super().__init__(baseurl, **keywords)

        self._mode = mode if mode in ("sync", "async") else "sync"
        self._uploads = UploadList.fromdict(uploads or {})

        self["REQUEST"] = "doQuery"
        self["LANG"] = language

        if maxrec:
            self["MAXREC"] = maxrec

        self["QUERY"] = query

        if self._uploads:
            self["UPLOAD"] = self._uploads.param()

    @property
    def queryurl(self):
        return '{baseurl}/{mode}'.format(baseurl=self.baseurl, mode=self._mode)

    def execute_stream(self):
        """
        submit the query and return the raw VOTable XML as a file stream

        Raises
        ------
        DALServiceError
           for errors connecting to or communicating with the service
        DALQueryError
           for errors in the input query syntax
        """

        # theres nothing to execute in non-sync queries
        if self._mode != "sync":
            raise DALServiceError(
                "Cannot execute a non-synchronous query. Use submit instead")

        return super().execute_stream()

    def execute(self):
        """
        submit the query and return the results as a TAPResults instance

        Raises
        ------
        DALServiceError
           for errors connecting to or communicating with the service
        DALQueryError
           for errors either in the input query syntax or
           other user errors detected by the service
        DALFormatError
           for errors parsing the VOTable response
        """
        return TAPResults(self.execute_votable(), url=self.queryurl)

    def submit(self):
        """
        Does the request part of the TAP query.
        This function is separated from response parsing because async queries
        return no votable but behave like sync queries in terms of request.
        It returns the requests response.
        """
        url = self.queryurl

        files = {
            upload.name: upload.fileobj()
            for upload in self._uploads
            if upload.is_inline
        }

        response = session.post(
            url, data=self, stream=True, files=files)
        # requests doesn't decode the content by default
        response.raw.read = partial(response.raw.read, decode_content=True)
        return response


class TAPResults(DatalinkResultsMixin, DALResults):
    """
    The list of matching images resulting from an image (SIA) query.
    Each record contains a set of metadata that describes an available
    image matching the query constraints.  The number of records in
    the results is available via the :py:attr:`nrecs` attribute or by
    passing it to the Python built-in ``len()`` function.

    This class supports iterable semantics; thus,
    individual records (in the form of
    :py:class:`~pyvo.dal.query.Record` instances) are typically
    accessed by iterating over an ``TAPResults`` instance.

    Alternatively, records can be accessed randomly via
    :py:meth:`getrecord` or through a Python Database API (v2)
    Cursor (via :py:meth:`~pyvo.dal.query.DALResults.cursor`).
    Column-based data access is possible via the
    :py:meth:`~pyvo.dal.query.DALResults.getcolumn` method.

    ``TAPResults`` is essentially a wrapper around an Astropy
    :py:mod:`~astropy.io.votable`
    :py:class:`~astropy.io.votable.tree.Table` instance where the
    columns contain the various metadata describing the images.
    One can access that VOTable directly via the
    :py:attr:`~pyvo.dal.query.DALResults.votable` attribute.  Thus,
    when one retrieves a whole column via
    :py:meth:`~pyvo.dal.query.DALResults.getcolumn`, the result is
    a Numpy array.  Alternatively, one can manipulate the results
    as an Astropy :py:class:`~astropy.table.table.Table` via the
    following conversion:

    >>> table = results.table

    ``SIAResults`` supports the array item operator ``[...]`` in a
    read-only context.  When the argument is numerical, the result
    is an
    :py:class:`~pyvo.dal.query.Record` instance, representing the
    record at the position given by the numerical index.  If the
    argument is a string, it is interpreted as the name of a column,
    and the data from the column matching that name is returned as
    a Numpy array.
    """

    @property
    def infos(self):
        """
        return the info element as dictionary
        """
        return getattr(self, "_infos", {})

    @property
    def query_status(self):
        """
        return the query status
        """
        return getattr(self, "_infos", {}).get("QUERY_STATUS", None)

    def getrecord(self, index):
        """
        return a representation of a tap result record that follows
        dictionary semantics. The keys of the dictionary are those returned by
        this instance's fieldnames attribute. The returned record has
        additional image-specific properties

        Parameters
        ----------
        index : int
           the integer index of the desired record where 0 returns the first
           record

        Returns
        -------
        REc
           a dictionary-like wrapper containing the result record metadata.

        Raises
        ------
        IndexError
           if index is negative or equal or larger than the number of rows in
           the result table.

        See Also
        --------
        Record
        """
        return TAPRecord(self, index)


class TAPRecord(SodaRecordMixin, DatalinkRecordMixin, Record):
    pass
