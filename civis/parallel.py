"""Parallel computations using the Civis Platform infrastructure
"""
from __future__ import absolute_import

from concurrent.futures import wait
from datetime import datetime, timedelta
from io import BytesIO
import logging
import os
import time

import joblib
from joblib._parallel_backends import ParallelBackendBase
from joblib.my_exceptions import TransportableException
import requests

import civis
from civis.base import CivisAPIError

from civis.compat import TemporaryDirectory
from civis.futures import _ContainerShellExecutor, CustomScriptExecutor

log = logging.getLogger(__name__)
_THIS_DIR = os.path.dirname(os.path.realpath(__file__))
_DEFAULT_SETUP_CMD = ":"  # An sh command that does nothing.
_DEFAULT_REPO_SETUP_CMD = "cd /app; python setup.py install; cd /"
_ALL_JOBS = 50  # Give the user this many jobs if they request "all of them"


def infer_backend_factory(required_resources=None,
                          params=None,
                          arguments=None,
                          client=None,
                          polling_interval=None,
                          setup_cmd=None,
                          max_submit_retries=0,
                          max_job_retries=0,
                          hidden=True):
    """Infer the container environment and return a backend factory.

    This function helps you run additional jobs from code which executes
    inside a Civis container job. The function reads settings for
    relevant parameters (e.g. the Docker image) of the container
    it's running inside of.

    .. note:: This function will read the state of the parent
    container job at the time this function executes. If the
    user has modified the container job since the run started
    (e.g. by changing the GitHub branch in the container's GUI),
    this function may infer incorrect settings for the child jobs.

    Parameters
    ----------
    required_resources : dict or None, optional
        The resources needed by the container. See the
        `container scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details. Resource requirements not specified will
        default to the requirements of the current job.
    params : list or None, optional
        A definition of the parameters this script accepts in the
        arguments field. See the `container scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details.
            Parameters of the child jobs will default to the parameters
        of the current job. Any parameters provided here will override
        parameters of the same name from the current job.
    arguments : dict or None, optional
        Dictionary of name/value pairs to use to run this script.
        Only settable if this script has defined params. See the `container
        scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details.
            Arguments will default to the arguments of the current job.
        Anything provided here will override portions of the current job's
        arguments.
    client : `civis.APIClient` instance or None, optional
        An API Client object to use.
    polling_interval : int, optional
        The polling interval, in seconds, for checking container script status.
        If you have many jobs, you may want to set this higher (e.g., 300) to
        avoid `rate-limiting <https://platform.civisanalytics.com/api#basics>`.
        You should only set this if you aren't using ``pubnub`` notifications.
    setup_cmd : str, optional
        A shell command or sequence of commands for setting up the environment.
        These will precede the commands used to run functions in joblib.
        This is primarily for installing dependencies that are not available
        in the dockerhub repo (e.g., "cd /app && python setup.py install"
        or "pip install gensim").
            With no GitHub repo input, the setup command will
        default to a command that does nothing. If a ``repo_http_uri``
        is provided, the default setup command will attempt to run
        "python setup.py install". If this command fails, execution
        will still continue.
    max_submit_retries : int, optional
        The maximum number of retries for submitting each job. This is to help
        avoid a large set of jobs failing because of a single 5xx error. A
        value higher than zero should only be used for jobs that are idempotent
        (i.e., jobs whose result and side effects are the same regardless of
        whether they are run once or many times).
    max_job_retries : int, optional
        Retry failed jobs this number of times before giving up.
        Even more than with ``max_submit_retries``, this should only
        be used for jobs which are idempotent, as the job may have
        caused side effects (if any) before failing.
        These retries assist with jobs which may have failed because
        of network or worker failures.
    hidden: bool, optional
        The hidden status of the object. Setting this to true
        hides it from most API endpoints. The object can still
        be queried directly by ID. Defaults to True.

    Raises
    ------
    RuntimeError
        If this function is not running inside a Civis container job.

    See Also
    --------
    civis.parallel.make_backend_factory
    """
    if client is None:
        client = civis.APIClient(resources='all')

    if not os.environ.get('CIVIS_JOB_ID'):
        raise RuntimeError('This function must be run '
                           'inside a container job.')
    state = client.scripts.get_containers(os.environ['CIVIS_JOB_ID'])
    if state.from_template_id:
        # If this is a Custom Script from a template, we need the
        # backing script. Make sure to save the arguments from
        # the Custom Script: those are the only user-settable parts.
        template = client.templates.get_scripts(state.from_template_id)
        try:
            custom_args = state.arguments
            state = client.scripts.get_containers(template.script_id)
            state.arguments = custom_args
        except civis.base.CivisAPIError as err:
            if err.status_code == 404:
                raise RuntimeError('Unable to introspect environment from '
                                   'your template\'s backing script. '
                                   'You may not have permission to view '
                                   'script ID {}.'.format(template.script_id))
            else:
                raise

    # Default to this container's resource requests, but
    # allow users to override it.
    state.required_resources.update(required_resources or {})

    # Update parameters with user input
    params = params or []
    for input_param in params:
        for param in state.params:
            if param['name'] == input_param['name']:
                param.update(input_param)
                break
        else:
            state.params.append(input_param)

    # Update arguments with input
    state.arguments.update(arguments or {})

    return make_backend_factory(docker_image_name=state.docker_image_name,
                                docker_image_tag=state.docker_image_tag,
                                repo_http_uri=state.repo_http_uri,
                                repo_ref=state.repo_ref,
                                required_resources=state.required_resources,
                                params=state.params,
                                arguments=state.arguments,
                                client=client,
                                polling_interval=polling_interval,
                                setup_cmd=setup_cmd,
                                max_submit_retries=max_submit_retries,
                                max_job_retries=max_job_retries,
                                hidden=hidden)


def make_backend_factory(docker_image_name="civisanalytics/datascience-python",
                         docker_image_tag="latest",
                         repo_http_uri=None,
                         repo_ref=None,
                         required_resources=None,
                         params=None,
                         arguments=None,
                         client=None,
                         polling_interval=None,
                         setup_cmd=None,
                         max_submit_retries=0,
                         max_job_retries=0,
                         hidden=True):
    """Create a joblib backend factory that uses Civis Container Scripts

    .. note:: The total size of function parameters in `Parallel()`
              calls on this backend must be less than 5 GB due to
              AWS file size limits.

    Parameters
    ----------
    docker_image_name : str, optional
        The image for the container script.
    docker_image_tag : str, optional
        The tag for the Docker image.
    repo_http_uri : str, optional
        The GitHub repo to check out to /app
        (e.g., github.com/my-user/my-repo.git)
    repo_ref : str, optional
        The GitHub repo reference to check out (e.g., "master")
    required_resources : dict or None, optional
        The resources needed by the container. See the
        `container scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details.
    params : list or None, optional
        A definition of the parameters this script accepts in the
        arguments field. See the `container scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details.
    arguments : dict or None, optional
        Dictionary of name/value pairs to use to run this script.
        Only settable if this script has defined params. See the `container
        scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details.
    client : `civis.APIClient` instance or None, optional
        An API Client object to use.
    polling_interval : int, optional
        The polling interval, in seconds, for checking container script status.
        If you have many jobs, you may want to set this higher (e.g., 300) to
        avoid `rate-limiting <https://platform.civisanalytics.com/api#basics>`.
        You should only set this if you aren't using ``pubnub`` notifications.
    setup_cmd : str, optional
        A shell command or sequence of commands for setting up the environment.
        These will precede the commands used to run functions in joblib.
        This is primarily for installing dependencies that are not available
        in the dockerhub repo (e.g., "cd /app && python setup.py install"
        or "pip install gensim").
            With no GitHub repo input, the setup command will
        default to a command that does nothing. If a `repo_http_uri`
        is provided, the default setup command will attempt to run
        "python setup.py install". If this command fails, execution
        will still continue.
    max_submit_retries : int, optional
        The maximum number of retries for submitting each job. This is to help
        avoid a large set of jobs failing because of a single 5xx error. A
        value higher than zero should only be used for jobs that are idempotent
        (i.e., jobs whose result and side effects are the same regardless of
        whether they are run once or many times).
    max_job_retries : int, optional
        Retry failed jobs this number of times before giving up.
        Even more than with `max_submit_retries`, this should only
        be used for jobs which are idempotent, as the job may have
        caused side effects (if any) before failing.
        These retries assist with jobs which may have failed because
        of network or worker failures.
    hidden: bool, optional
        The hidden status of the object. Setting this to true
        hides it from most API endpoints. The object can still
        be queried directly by ID. Defaults to True.

    Examples
    --------
    >>> # Without joblib:
    >>> from __future__ import print_function
    >>> from math import sqrt
    >>> print([sqrt(i ** 2) for i in range(10)])
    [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    >>> # Using the default joblib backend:
    >>> from joblib import delayed, Parallel
    >>> parallel = Parallel(n_jobs=5)
    >>> print(parallel(delayed(sqrt)(i ** 2) for i in range(10)))
    [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    >>> # Using the Civis backend:
    >>> from joblib import parallel_backend, register_parallel_backend
    >>> from civis.parallel import make_backend_factory
    >>> register_parallel_backend('civis', make_backend_factory(
    ...     required_resources={"cpu": 512, "memory": 256}))
    >>> with parallel_backend('civis'):
    ...    parallel = Parallel(n_jobs=5, pre_dispatch='n_jobs')
    ...    print(parallel(delayed(sqrt)(i ** 2) for i in range(10)))
    [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    >>> # Using scikit-learn with the Civis backend:
    >>> from sklearn.externals.joblib import \
    ...     register_parallel_backend as sklearn_register_parallel_backend
    >>> from sklearn.externals.joblib import \
    ...     parallel_backend as sklearn_parallel_backend
    >>> from sklearn.model_selection import GridSearchCV
    >>> from sklearn.ensemble import GradientBoostingClassifier
    >>> from sklearn.datasets import load_digits
    >>> digits = load_digits()
    >>> param_grid = {
    ...     "max_depth": [1, 3, 5, None],
    ...     "max_features": ["sqrt", "log2", None],
    ...     "learning_rate": [0.1, 0.01, 0.001]
    ... }
    >>> # Note: n_jobs and pre_dispatch specify the maximum number of
    >>> # concurrent jobs.
    >>> gs = GridSearchCV(GradientBoostingClassifier(n_estimators=1000,
    ...                                              random_state=42),
    ...                   param_grid=param_grid,
    ...                   n_jobs=5, pre_dispatch="n_jobs")
    >>> sklearn_register_parallel_backend('civis', make_backend_factory(
    ...     required_resources={"cpu": 512, "memory": 256}))
    >>> with sklearn_parallel_backend('civis'):
    ...     gs.fit(digits.data, digits.target)

    Notes
    -----
    Joblib's ``register_parallel_backend`` (see example above) expects a
    callable that returns a ``ParallelBackendBase`` instance. This function
    allows the user to specify the Civis container script setting that will be
    used when that backend creates container scripts to run jobs.

    The specified Docker image (optionally, with a GitHub repo and setup
    command) must have basically the same environment as the one in which this
    module is used to submit jobs. The worker jobs need to be able to
    deserialize the jobs they are given, including the data and all the
    necessary Python objects (e.g., if you pass a Pandas data frame, the image
    must have Pandas installed). In particular, the function that is called by
    ``joblib`` must be available in the specified environment.
    """
    if setup_cmd is None:
        if repo_http_uri is not None:
            setup_cmd = _DEFAULT_REPO_SETUP_CMD
        else:
            setup_cmd = _DEFAULT_SETUP_CMD

    def backend_factory():
        return _CivisBackend(docker_image_name=docker_image_name,
                             docker_image_tag=docker_image_tag,
                             repo_http_uri=repo_http_uri,
                             repo_ref=repo_ref,
                             required_resources=required_resources,
                             params=params,
                             arguments=arguments,
                             client=client,
                             polling_interval=polling_interval,
                             setup_cmd=setup_cmd,
                             max_submit_retries=max_submit_retries,
                             max_n_retries=max_job_retries,
                             hidden=hidden)

    return backend_factory


def make_backend_template_factory(from_template_id,
                                  arguments=None,
                                  client=None,
                                  polling_interval=None,
                                  max_submit_retries=0,
                                  max_job_retries=0,
                                  hidden=True):
    """Create a joblib backend factory that uses Civis Custom Scripts.

    Parameters
    ----------
    from_template_id: int
        Create jobs as Custom Scripts from the given template ID.
        When using the joblib backend with templates,
        the template must have a very specific form. Refer
        to the README for details.
    arguments : dict or None, optional
        Dictionary of name/value pairs to use to run this script.
        Only settable if this script has defined params. See the `container
        scripts API documentation
        <https://platform.civisanalytics.com/api#resources-scripts>`
        for details.
    client : `civis.APIClient` instance or None, optional
        An API Client object to use.
    polling_interval : int, optional
        The polling interval, in seconds, for checking container script status.
        If you have many jobs, you may want to set this higher (e.g., 300) to
        avoid `rate-limiting <https://platform.civisanalytics.com/api#basics>`.
        You should only set this if you aren't using ``pubnub`` notifications.
    max_submit_retries : int, optional
        The maximum number of retries for submitting each job. This is to help
        avoid a large set of jobs failing because of a single 5xx error. A
        value higher than zero should only be used for jobs that are idempotent
        (i.e., jobs whose result and side effects are the same regardless of
        whether they are run once or many times).
    max_job_retries : int, optional
        Retry failed jobs this number of times before giving up.
        Even more than with `max_submit_retries`, this should only
        be used for jobs which are idempotent, as the job may have
        caused side effects (if any) before failing.
        These retries assist with jobs which may have failed because
        of network or worker failures.
    hidden: bool, optional
        The hidden status of the object. Setting this to true
        hides it from most API endpoints. The object can still
        be queried directly by ID. Defaults to True.
    """
    def backend_factory():
        return _CivisBackend(from_template_id=from_template_id,
                             arguments=arguments,
                             client=client,
                             polling_interval=polling_interval,
                             max_submit_retries=max_submit_retries,
                             max_n_retries=max_job_retries,
                             hidden=hidden)

    return backend_factory


class JobSubmissionError(Exception):
    pass


def _robust_result_download(output_file_id, client, n_retries=5, delay=0.0):
    """Download and deserialize the result from output_file_id

    Retry network errors `n_retries` times with `delay` seconds between calls
    """
    retry_exc = (requests.HTTPError,
                 requests.ConnectionError,
                 requests.ConnectTimeout)
    n_failed = 0
    while True:
        buffer = BytesIO()
        try:
            civis.io.civis_to_file(output_file_id, buffer, client=client)
        except retry_exc as exc:
            buffer.close()
            if n_failed < n_retries:
                n_failed += 1
                log.debug("Download failure %s due to %s; retrying.",
                          n_failed, str(exc))
                time.sleep(delay)
            else:
                raise
        else:
            buffer.seek(0)
            return joblib.load(buffer)


class _CivisBackendResult:
    """A wrapper for results of joblib tasks

    This wrapper makes results look like the results from multiprocessing
    pools that joblib expects.  This retrieves the results for a completed
    job (i.e., container script) from Civis.

    Parameters
    ----------
    future : :class:`~civis.futures.ContainerFuture`
        A Future which represents a Civis job. Created by a
        :class:`~_ContainerShellExecutor`.
    callback : callable
        A `joblib`-provided callback function which should be
        called on successful job completion. It will launch the
        next job in line. See `joblib.parallel.Parallel._dispatch`
        for the creation of this callback function.
        It takes a single input, the output of the remote function call.

    Notes
    -----
    * This is similar to a Future object except with ``get`` instead of
      ``result``, and with a callback specified.
    * This is only intended to work within joblib and with the Civis backend.
    * Joblib calls ``get`` on one result at a time, in order of submission.
    * Exceptions should only be raised inside ``get`` so that joblib can
        handle them properly.
    """
    def __init__(self, future, callback):
        self._future = future
        self._callback = callback
        self.result = None
        if hasattr(future, 'client'):
            self._client = future.client
        else:
            self._client = civis.APIClient(resources='all')

        # Download results and trigger the next job as a callback
        # so that we don't have to wait for `get` to be called.
        # Note that the callback of a `concurrent.futures.Future`
        # (which self._future is a subclass of) is called with a
        # single argument, the Future itself.
        self._future.remote_func_output = None  # `get` reads results from here
        self._future.result_fetched = False  # Did we get the result?
        self._future.add_done_callback(
            self._make_fetch_callback(self._callback, self._client))

    @staticmethod
    def _make_fetch_callback(joblib_callback, client):
        """Create a closure for use as a callback on the ContainerFuture"""
        def _fetch_result(fut):
            """Retrieve outputs from the remote function.
            Run the joblib callback only if there were no errors.

            Parameters
            ----------
            fut : :class:`~civis.futures.ContainerFuture`
                A Future which represents a Civis job. Created by a
                :class:`~_ContainerShellExecutor`.

            Note
            ----
            We can't return data from a callback, so the remote
            function output is attached to the Future object
            as a new attribute ``remote_func_output``.
            """
            if fut.succeeded():
                log.debug(
                    "Ran job through Civis. Job ID: %d, run ID: %d;"
                    " job succeeded!", fut.job_id, fut.run_id)
            elif fut.cancelled():
                log.debug(
                    "Ran job through Civis. Job ID: %d, run ID: %d;"
                    " job cancelled!", fut.job_id, fut.run_id)
            else:
                log.error(
                    "Ran job through Civis. Job ID: %d, run ID: %d;"
                    " job failure!", fut.job_id, fut.run_id)

            try:
                # Find the output file ID from the run outputs.
                run_outputs = client.scripts.list_containers_runs_outputs(
                    fut.job_id, fut.run_id)
                if run_outputs:
                    output_file_id = run_outputs[0]['object_id']
                    res = _robust_result_download(output_file_id, client,
                                                  n_retries=5, delay=1.0)
                    fut.remote_func_output = res
                    log.debug("Downloaded and deserialized the result.")
            except BaseException as exc:
                # If something went wrong when fetching outputs, record the
                # exception so we can re-raise it in the parent process.
                # Catch BaseException so we can also re-raise a
                # KeyboardInterrupt where it can be properly handled.
                log.debug('Exception during result download: %s', str(exc))
                fut.remote_func_output = exc
            else:
                fut.result_fetched = True
                if not fut.cancelled() and not fut.exception():
                    # The next job will start when this callback is called.
                    # Only run it if the job was a success.
                    joblib_callback(fut.remote_func_output)

        return _fetch_result

    def get(self):
        """Block and return the result of the job

        Returns
        -------
        The output of the function which ``joblib`` ran via Civis
            NB: ``joblib`` expects that ``get`` will always return an iterable.
        The remote function(s) should always be wrapped in
        ``joblib.parallel.BatchedCalls``, which does always return a list.

        Raises
        ------
        TransportableException
            Any error in the remote job will result in a
            ``TransportableException``, to be handled by ``Parallel.retrieve``.
        futures.CancelledError
            If the remote job was cancelled before completion
        """
        if self.result is None:
            # Wait for the script to complete.
            wait([self._future])
            self.result = self._future.remote_func_output

        if self._future.exception() or not self._future.result_fetched:
            # If the job errored, we may have been able to return
            # an exception via the run outputs. If not, fall back
            # to the API exception.
            # Note that a successful job may still have an exception
            # result if job output retrieval failed.
            if self.result is not None:
                raise self.result
            else:
                # Use repr for the message because the API exception
                # typically has str(exc)==None.
                exc = self._future.exception()
                raise TransportableException(repr(exc), type(exc))

        return self.result


class _CivisBackend(ParallelBackendBase):
    """The backend class that tells joblib how to use Civis to run jobs

    Users should interact with this through ``make_backend_factory``.
    """
    def __init__(self, setup_cmd=_DEFAULT_SETUP_CMD,
                 from_template_id=None,
                 max_submit_retries=0,
                 client=None,
                 **executor_kwargs):
        if max_submit_retries < 0:
            raise ValueError(
                "max_submit_retries cannot be negative (value = %d)" %
                max_submit_retries)

        if client is None:
            client = civis.APIClient(resources='all')
        self._client = client
        if from_template_id:
            self.executor = CustomScriptExecutor(from_template_id,
                                                 client=client,
                                                 **executor_kwargs)
        else:
            self.executor = _ContainerShellExecutor(client=client,
                                                    **executor_kwargs)
        self.setup_cmd = setup_cmd
        self.max_submit_retries = max_submit_retries
        self.using_template = (from_template_id is not None)

    def effective_n_jobs(self, n_jobs):
        if n_jobs == -1:
            n_jobs = _ALL_JOBS
        if n_jobs <= 0:
            raise ValueError("Please request a positive number of jobs, "
                             "or use \"-1\" to request a default "
                             "of {} jobs.".format(_ALL_JOBS))
        return n_jobs

    def abort_everything(self, ensure_ready=True):
        # This method is called when a job has raised an exception.
        # In that case, we're not going to finish computations, so
        # we should free up Platform resources in any remaining jobs.
        self.executor.cancel_all()
        if not ensure_ready:
            self.executor.shutdown(wait=False)

    def apply_async(self, func, callback=None):
        """Schedule func to be run
        """
        # Serialize func to a temporary file and upload it to a Civis File.
        # Make the temporary files expire in a week.
        expires_at = (datetime.now() + timedelta(days=7)).isoformat()
        with TemporaryDirectory() as tempdir:
            temppath = os.path.join(tempdir, "civis_joblib_backend_func")
            # compress=3 is a compromise between space and read/write times
            # (https://github.com/joblib/joblib/blob/18f9b4ce95e8788cc0e9b5106fc22573d768c44b/joblib/numpy_pickle.py#L358).
            joblib.dump(func, temppath, compress=3)
            with open(temppath, "rb") as tmpfile:
                func_file_id = \
                    civis.io.file_to_civis(tmpfile,
                                           "civis_joblib_backend_func",
                                           expires_at=expires_at,
                                           client=self._client)
                log.debug("uploaded serialized function to File: %d",
                          func_file_id)

            # Use the Civis CLI client to download the job runner script into
            # the container, and then run it on the uploaded job.
            # Only download the runner script if it doesn't already
            # exist in the destination environment.
            runner_remote_path = "civis_joblib_worker"
            cmd = ("{setup_cmd} && "
                   "if command -v {runner_remote_path} >/dev/null; "
                   "then exec {runner_remote_path} {func_file_id}; "
                   "else pip install civis=={civis_version} && "
                   "pip install joblib=={jl_version} && "
                   "exec {runner_remote_path} {func_file_id}; fi"
                   .format(jl_version=joblib.__version__,
                           civis_version=civis.__version__,
                           runner_remote_path=runner_remote_path,
                           func_file_id=func_file_id,
                           setup_cmd=self.setup_cmd))

            # Try to submit the command, with optional retrying for certain
            # error types.
            for n_retries in range(1 + self.max_submit_retries):
                try:
                    if self.using_template:
                        args = {'JOBLIB_FUNC_FILE_ID': func_file_id}
                        future = self.executor.submit(**args)
                        log.debug("Started custom script from template "
                                  "%s with arguments %s",
                                  self.executor.from_template_id, args)
                    else:
                        future = self.executor.submit(fn=cmd)
                        log.debug("started container script with "
                                  "command: %s", cmd)
                    # Stop retrying if submission was successful.
                    break
                except CivisAPIError as e:
                    # If we've retried the maximum number of times already,
                    # then raise an exception.
                    retries_left = self.max_submit_retries - n_retries - 1
                    if retries_left < 1:
                        raise JobSubmissionError(e)

                    log.debug("Retrying submission. %d retries left",
                              retries_left)

                    # Sleep with exponentially increasing intervals in case
                    # the issue persists for a while.
                    time.sleep(2 ** n_retries)

            if self.executor.max_n_retries:
                # Start the ContainerFuture polling.
                # This will use more API calls, but will
                # allow the ContainerFuture to launch
                # retries if necessary.
                # (This is only relevant if we're not using the
                # notifications endpoint.)
                future.done()

            result = _CivisBackendResult(future, callback)

        return result
