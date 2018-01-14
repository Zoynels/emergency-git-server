#!/usr/bin/python3
# -*- coding: UTF-8 -*-
r"""Usage::

    python3 emergency_git_server.py [DOCROOT]

        DOCROOT
            Path to root URI exposed by server, commonly /var/www/html;
            defaults to current working directory; repos must reside at
            least two levels below this, e.g., /var/www/html/1/repo.git


    Environment variables - all are unset by default

        GITSRV_PREFIX <str>
            Prefix for the following env vars; defaults to _ (shown)

        _HOST <hostname>
            IP address or hostname; defaults to localhost

        _PORT <port>
            Port number; defaults to 8000 (probably a good idea to keep
            it above 1023)

        _LOGFILE <path>
            Redirect all server messages (from standard error) to path;
            <path> need not exist and is truncated at startup

        _DEBUG
            Print verbose logging info for every request/response

        _ENFORCE_DOTGIT
            Existing or newly initialized repos must end in .git (on the
            server side)

        _CREATE_MISSING
            Allow cloning and pushing of non-existent repos, like so:

            $ git clone http://localhost:8000/git_root/myrepo.git

            or ...

            $ cd existing_repo
            $ git remote add origin \
                 http://localhost:8000/git_root/existing_repo.git
            $ git push -u origin master

            Note: HEAD is currently left unset.

        _FIRST_CHILD_OK
            Override the level-2+ depth requirement noted above, i.e.,
            allow first-child repos; the requirement itself is a legacy
            holdover from the cgi-bin days and will probably be removed
            or enabled by default if this script ever gets a makeover

        _USE_NAMESPACES
            Interpret non-existent path components between DOCROOT/real
            and the target repo (exclusive) as $GIT_NAMESPACE, e.g.,
            /var/www/html/git_root/$GIT_NAMESPACE/myrepo.git; see
            gitnamespaces(7)

        _AUTHFILE <path|str>
            Enforce "basic access" authentication. This can either be an
            absolute path to a .json file or a stringified json-style
            "object" with these fields:
            {
              "/some/path/below/docroot": {
                "description": STR, optional message or realm name
                "secretsfile": STR, required abs path to .htpasswd-like file
                "realaccount": BOOL, optional override for _REQURE_ACCOUNT
                "privaterepo": BOOL, optional; deny public read access
              }, ...
            }

        _REQURE_ACCOUNT (not implemented)
            Enforce a real account policy. Users named in the _AUTHFILE must
            have an existing account on the server. System permission are
            checked before access to files are granted.

        _CERTFILE <path>
            Path to an x509 cert in PEM format or a combined cert plus RSA
            key (which must appear first). If the path is valid, the server
            will listen on port 4443 (if PORT is 8000 or not specified).
            Note: certificate chains must end with a root cert.

        _KEYFILE <path>
            Path to a valid RSA key in PEM format. If absent, CERTFILE must
            contain the key.

        _DHPARAMS <path>
            Path to an optional DH parameter file, also in PEM format.


Notes
-----

This is a minimal, sequential, "single-serving" Git server geared toward
emergency use, ad hoc experimentation, and basic Git education. General use is
strongly discouraged, not least because of a total lack of attention paid to
matters of security and performance, which completely overshadow the various
limitations and vulnerabilities laid out in ``http.server``, ``socketserver``,
and related modules upon which this is based (mostly regarding the use of
blocking sockets and file-like objects, and known risks like arbitrary/remote
code injection/execution.)

While it's possible, say, to run this behind a reverse proxy to ensure more
legit auth/auth and TLS handling, that's really missing the point. Faster,
lighter, and more robust options exist, and some don't rely on a local git
installation. At the very least, you'd want something that supports some flavor
of concurrency. Professional tools combining libgit2 with twisted or gevent or
asyncio do exactly this (not to mention those offered by other languages).

If vast portions of this script come off as roundabout and confusing, that's in
part due to these factors:

1. A fair bit of request twiddling is necessary to conform to the traditional
   CGI interface presented by the git-http-backend_ utility, the reference
   implementation for CGI scripts that comes standard in most Git installations
2. Sheer ignorance/incompetence on the author's part regarding compatibility
   concerns and standards conformance (spec_), a deficiency that's inspired the
   kitchen-sink approach taken below

The rest of these notes address some quirks encountered in delegating to
git-http-backend. It should be of no interest to most users.


Dealing with git-http-backend
-----------------------------

Confusingly, Git's client-side commands utilize at least two different URL
request syntaxes re CGI scripts.

1. ``/git_root/myrepo.git/info/refs?service=git-cmd``
2. ``/git_root/myrepo.git/git-cmd``

Here, ``git_root`` is merely a stand-in for the leading components of the
target Git repo's URI. The first thing to note is that the main CGI command
spawned by the server as a child process never changes::

    /usr/libexec/git-core/git-http-backend

This is sometimes exported as ``$SCRIPT_FILENAME``, along with a shorter,
tail-only (basename) version in ``$SCRIPT_NAME``, which would expand to
``git-http-backend``. Likewise, ``$GIT_PROJECT_ROOT`` is always set to::

    $DOCROOT/git_root

... where ``$DOCROOT`` is something like ``/var/www`` and ``git_root`` the
intermediate dirs between ``$DOCROOT`` and the Git repo. The only real
difference between forms (1) and (2) lies in how they impact the value of
two environment variables::

    # (1) GET /git_root/myrepo.git/info/refs?service=git-upload-pack HTTP/1.1
    $PATH_INFO    == "/myrepo.git/info/refs"
    $QUERY_STRING == "service=git-upload-pack"

    # (2) POST /git_root/myrepo.git/git-upload-pack HTTP/1.1
    $PATH_INFO    == "/myrepo.git/git-upload-pack"
    $QUERY_STRING == ""

.. _spec: http://www.ietf.org/rfc/rfc3875
.. _git-http-backend: https://github.com/git/git
   /Documentation/git-http-backend.txt

"""
# Author: Jane Soko
# License: Apache License 2.0
# Portions derived from Python modules may apply other terms.
# See <https://docs.python.org/3.5/license.html> for details.
#
# TODO All HTTPStatus codes are naively assigned and largely misapplied. Use
# official IANA RFC when revising.
# TODO clarify path-translation behavior via unit tests, then decouple
# TODO feature: offer command-line options in addition to environment variables
# TODO feature: have FIRST_CHILD_OK be on by default

from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import

import sys
import copy
import os
import re
import select

from subprocess import (check_output, list2cmdline, Popen,
                        PIPE, CalledProcessError)

if sys.version_info < (3, 0):
    import httplib as HTTPStatus
    import urlparse
    from future_builtins import filter
    from CGIHTTPServer import CGIHTTPRequestHandler, _url_collapse_path
    from BaseHTTPServer import HTTPServer
else:
    import urllib.parse as urlparse
    from http import HTTPStatus
    from http.server import (CGIHTTPRequestHandler, HTTPServer,
                             _url_collapse_path)


# Real, local path exposed by server as '/'. Full dereferencing with
# os.path.realpath() might not be desirable in some situations.
DOCROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 and
                          os.path.isdir(sys.argv[1]) else ".")
envvar_prefix = os.getenv("GITSRV_PREFIX", "_")


def boolify_envvar(envvar, prefix=envvar_prefix):
    """ Interpret boolean environment variables as True (whenever
    set/exported), even if their value is an empty string, "null", or
    "None". Compare to ``bool(os.getenv('MYENVVAR', False))``.

    """
    # "nil" is a tossup; could mean nihil (0) or false, like Lisp's ``NIL``.
    return (os.getenv(prefix + envvar, "false").lower() not in
            "false nil no off 0".split())


HOST = os.getenv(envvar_prefix + "HOST", "localhost")
PORT = int(os.getenv(envvar_prefix + "PORT", 8000))
LOGFILE = os.getenv(envvar_prefix + "LOGFILE")
AUTHFILE = os.getenv(envvar_prefix + "AUTHFILE")
DEBUG = boolify_envvar("DEBUG")
ENFORCE_DOTGIT = boolify_envvar("ENFORCE_DOTGIT")
CREATE_MISSING = boolify_envvar("CREATE_MISSING")
FIRST_CHILD_OK = boolify_envvar("FIRST_CHILD_OK")
USE_NAMESPACES = boolify_envvar("USE_NAMESPACES")
REQURE_ACCOUNT = boolify_envvar("REQURE_ACCOUNT")

CERTFILE = os.getenv(envvar_prefix + "CERTFILE")
KEYFILE = os.getenv(envvar_prefix + "KEYFILE")
DHPARAMS = os.getenv(envvar_prefix + "DHPARAMS")


def get_libexec_dir():
    """Return path to dir containing Git plumbing exes"""
    out_path = None
    #
    try:
        out_path = check_output(("git", "--exec-path"))
    except CalledProcessError:
        out_path = "/usr/libexec/git-core"
    except FileNotFoundError:
        try:
            out_path = check_output("command -p git --exec-path".split())
        except FileNotFoundError:
            print("Could not locate primary git program in $PATH",
                  file=sys.stderr)
            raise
    else:
        out_path = out_path.decode().strip()
    return out_path


def get_auth_dict():
    if AUTHFILE is None:
        return {}
    import json
    if os.path.exists(AUTHFILE):
        with open(AUTHFILE) as f:
            outdict = json.load(f)
    else:
        outdict = json.loads(AUTHFILE)
    return outdict


class CtxServer(HTTPServer, object):
    """Use standard per-request wrapping rather than wrap the bound
    listen socket as an instance attribute prior to starting the loop.
    The latter approach actually seems to work just as well, but this
    mimics the example given in the docs_. Would be nice to get rid of
    this class, though.

    BTW, that example also uses ``socket.SHUT_RDWR`` instead of the
    write-only variant, used here.  But read-write seems to trigger "bad
    FD" errors during error handling.

    .. _docs: https://docs.python.org/3.6/library
       /ssl.html#server-side-operation
    """
    def __init__(self, server_address, RequestHandlerClass, context=None):
        self.ssl_context = context
        super(CtxServer, self).__init__(server_address, RequestHandlerClass,
                                        bind_and_activate=True)

    def _handle_request_noblock(self):
        """No idea how this really works. See superclass docstring.
        """
        try:
            request, client_address = self.get_request()
        except OSError:
            return
        if self.verify_request(request, client_address):
            try:
                if self.ssl_context:
                    try:
                        request = self.ssl_context.wrap_socket(
                            request, server_side=True)
                    except Exception as e:
                        import ssl
                        # Usually means client hasn't okay'd self-signed certs
                        if isinstance(e, ssl.SSLError):
                            print("%r" % e, file=sys.stderr)
                            self.shutdown_request(request)
                            return
                        else:
                            raise
                self.process_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
                self.shutdown_request(request)
            except:  # noqa: E722
                self.shutdown_request(request)
                raise
        else:
            self.shutdown_request(request)


class HTTPBackendHandler(CGIHTTPRequestHandler, object):
    """The included CGI handler from the standard library needs a bit of
    massaging to play nice with git-http-backend(1). See the main module's
    __doc__ for details.
    """

    has_openssl = None
    git_exec_path = get_libexec_dir()
    auth_dict = get_auth_dict()
    docroot = DOCROOT
    get_RE = re.compile(r'^/.+/objects/'
                        r'(pack/pack-[0-9a-f]{40}\.(pack|idx)|'
                        r'[0-9a-f]{2}/[0-9a-f]{38})$')

    def dlog(self, fmt, *msg, **kwargs):
        """ This prints concatenated args and pretty-prints kwargs. It
        uses the ``super().log_message`` method, which just prints to
        stderr without summoning the logging module.
        """
        if not DEBUG:
            return
        import inspect
        ctx = inspect.stack()[1]
        if hasattr(ctx, "function"):
            out = ["%s()" % ctx.function, " - "]
        else:
            out = ["%s()" % ctx[3], " - "]
        #
        if all(c in fmt for c in "{}"):
            out.append(fmt.format(*msg))
        elif any("%%%c" % c in fmt for c in "srcdiouxfFgGXeEa#0- +"):
            out.append(fmt % msg)
        elif fmt:
            out.append(" ".join((fmt,) + msg))
        else:
            out.pop()
        if kwargs.get("locals"):
            # Can also get class info with:
            #   ``dict(inspect.getmembers(locs["self"]))``
            locs = ctx.frame.f_locals
            for key in kwargs["locals"]:
                if key.startswith("self"):
                    # Guess this func's ``self`` doesn't work here?
                    val = getattr(locs["self"], key.partition("self.")[-1],
                                  "Error: missing")
                else:
                    val = locs.get(key, "Error: missing")
                kwargs.update({key: val})
            del kwargs["locals"]
        #
        if kwargs:
            maxlen = max(len(k) for k in kwargs) + 1
            out += ["\n{:2}{:<{w}} {!r}".format("", k + ":", v, w=maxlen) for
                    k, v in kwargs.items()]
        # ``BaseHTTPRequestHandler.log_message`` takes printf syntax, so just
        # concat, then disregard entirely. A stray ``%s`` shouldn't bother.
        self.log_message("".join(out))

    def is_cgi(self):
        """This modified version of ``is_cgi`` still performs the same
        basic function as its Super, but the ancillary ``cgi_info`` var
        has been renamed to ``repo_info`` to better distinguish between
        the aliased Git CGI scripts dir (``/usr/libexec/git-core``) and
        the Git repo itself.

        Namespaces
        ----------
        Initial cloning from a remote repo with existing namespaces
        takes some extra setup, which is another way of saying
        something's broken here.  When including an existing namespace
        as the repo's prefix in the url arg to ``git clone``, this
        warning appears: ``warning: remote HEAD refers to nonexistent
        ref, unable to checkout.`` And the cloned work tree is empty
        until issuing a ``git pull origin master``.

        Upon updating ``remote.origin.url`` with a new namespace prefix,
        and pushing, everything seems okay. The remote HEAD is even
        updated to point to the new namespaced ref. It's as if ``git
        symbolic-ref`` were issued manually on the server side.

        When cloning without any (existing) namespace prefixing the repo
        component of the url, a familiar refrain appears::

            Note: checking out '2641d08..'
            You are in 'detached HEAD' state. You can look around ...
            ...
            git checkout -b <new-branch-name>

        And ``status`` says ``not on any branch``. Checking out master
        without the ``-b`` miraculously puts everything in sync. After
        updating the remote url and issuing a ``push -u origin master``,
        the new namespace is created successfully on the remote.

        Update 1. -- it seems most of the above only applies to remotes
        that were initialized without the normal refs/heads/master but
        whose HEAD still pointed thus before being pushed to.
        """
        # TODO migrate all parsing business from ``run_cgi()`` up here.  The
        # basic purpose of this function, which is to split the path into head
        # and tail components, is redundant because ``run_cgi`` does it again.
        #
        # XXX ``SimpleHTTPRequestHandler`` calls ``posixpath.normpath``, which
        # seems pretty similar to ``http.server._url_collapse_path``. Might be
        # worth checking out.  Guessing this one has to do with deterring
        # pardir URI mischief, but not certain.
        collapsed_path = _url_collapse_path(self.path)
        git_root, tail = self.find_repo(collapsed_path)
        # Attempt to accommodate namespaced setups. This must occur before
        # non-existent dirs are interpreted as "missing."
        ns = None
        if USE_NAMESPACES:
            ns_test = self.find_repo(collapsed_path, allow_fake=True)
            self.dlog("is_cgi - ns_test:", USE_NAMESPACES=USE_NAMESPACES,
                      ns_test=ns_test, git_root=git_root)
            if ns_test[0] != git_root:
                git_root, tail = ns_test
                git_root, ns = self.find_repo(git_root)
                #
                # Here, the intent is surely to initialize a new repo.
                if git_root == "/" and "/" in ns:
                    tail = ns + "/" + tail
                    ns = None
                # For GET requests, ``self.path`` must be rewritten to prevent
                # 404s by aiding ``SimpleHTTPRequestHandler`` find the repo.
                else:
                    self.path = collapsed_path = (git_root.rstrip("/") +
                                                  "/" + tail.lstrip("/"))
        self.dlog("is_cgi - top", git_root=git_root, ns=ns, tail=tail)
        if ns:
            nsrepo = os.path.join(git_root.lstrip("/"), tail.partition("/")[0])
            nspath = os.path.join(self.docroot, nsrepo)
            try:
                nshead = check_output(("git -C %s symbolic-ref HEAD" %
                                       nspath).split())
            except CalledProcessError as e:
                self.log_error("{!r}".format(e))
            else:
                self.dlog("is_cgi: %s/HEAD:" % nsrepo, nshead=nshead.decode())
        #
        # Disqualify GET requests for static resources in ``$GIT_DIR/objects``.
        if self.get_RE.match(collapsed_path):
            return False
        # XXX a temporary catch-all to handle requests for extant paths that
        # don't resolve to ``$GIT_DIR/objects``. Separating this block from the
        # RE block above is a lazy way of acknowledging that simply dropping
        # such requests outright or throwing errors might be preferable to
        # having ``send_head`` shunt them to ``SimpleHTTPRequestHandler``. Any
        # such logic, if/when needed, should go here.
        cgi_cand = os.path.join(self.docroot, collapsed_path.strip('/'))
        if os.path.exists(cgi_cand):
            return False
        #
        # Enforce a "CGI-bin present" policy to allow for easier integration of
        # external authorization facilities.  Permissions problems may arise if
        # overridden, i.e., if ``FIRST_CHILD_OK == True``.
        if git_root == "/":
            # This should only run if all components have yet to be created or
            # if the topmost (1st child) is an existing repo.
            #
            gr_test = self.find_repo(collapsed_path, allow_fake=True)
            gr_test = self.find_repo(gr_test[0])
            msg = None
            mutate_path = False
            if gr_test[0] == "/" and not self.is_repo(os.path.join(
                    self.docroot, tail.lstrip("/").split("/")[0])):
                # This is for dry clones, so no component can actually exist...
                #
                if CREATE_MISSING is False:
                    # ... and none will be created
                    msg = "The requested path could not be found " \
                        "and the env var _CREATE_MISSING is not set"
                elif ('/info/refs?service=' in tail and '/' not
                        in tail.split("/info/refs?service=")[0].lstrip("/")):
                    # A lone, first-child of docroot has been requested
                    if FIRST_CHILD_OK is True:
                        # Let ``CREATE_MISSING`` logic below christen it a repo
                        mutate_path = True
                    else:
                        msg = "CREATE_MISSING is set but path is only one" \
                            " level deep; set _FIRST_CHILD_OK to override"
                else:
                    # Multiple components wanted, so this can fall through.
                    #
                    # XXX note this doesn't check for the presence of a
                    # ``service`` query string or that the method is ``GET``.
                    pass
            else:
                # First component indeed exists and is a git repo
                if FIRST_CHILD_OK:
                    mutate_path = True
                else:
                    msg = """\n
                    =============== WARNING ===============
                    The requested Git repo should not be a
                    first child of the root URI, "/"; to
                    override, export "_FIRST_CHILD_OK=1";
                    see usage; hit Ctrl-C (SIGINT) to exit
                    """
            self.dlog("is_cgi - git_root missing",
                      gr_test=gr_test,
                      tail=tail,
                      mutate_path=mutate_path,
                      docroot=self.docroot,
                      collapsed_path=collapsed_path)
            if msg is not None:
                self.send_error(HTTPStatus.FORBIDDEN, msg)
                # Raise exception so msg is prominent in server-side logs
                raise ValueError(msg)  # no ConnectionError in 2.7
            elif mutate_path is True:
                # Nest everything by a level (break out of DOCROOT)
                self.docroot, git_root = os.path.split(self.docroot)
                collapsed_path = '/' + git_root + collapsed_path
        #
        dir_sep = collapsed_path.find('/', 1)
        #
        # NOTE - this resets everything -- the stuff above merely weeds out the
        # corner cases.
        #
        # ``head`` = 1st component of ``self.path`` w/o trailing slash
        # ``tail`` = the rest, no leading slash
        #
        # This split is only a starting point, or baseline, to allow the
        # setting of initial values for ``root``, ``repo``, etc.
        head, tail = collapsed_path[:dir_sep], collapsed_path[dir_sep+1:]
        #
        self.repo_info = head, tail, ns
        #
        # Attempt to create repo if it doesn't exist; applies to both upload
        # and receive requests
        if (CREATE_MISSING is True and '/info/refs?service=' in tail):
            uri = os.path.join(
                self.docroot,
                collapsed_path.split('/info/refs?service=')[0].strip('/'))
            try:
                # Assume mode is set according to umask
                os.makedirs(uri)
            except FileExistsError:
                pass
            # Target repo be empty
            if len(os.listdir(uri)) == 0:
                try:
                    cp = check_output(('git -C %s init --bare' % uri).split())
                except CalledProcessError as e:
                    self.log_error('%r', e)
                else:
                    self.dlog('is_cgi - created new repo', cp=cp)
        self.dlog("is_cgi:",
                  **{"self.raw_requestline": self.raw_requestline,
                     "collapsed_path": collapsed_path,
                     "git_root": self.find_repo(collapsed_path)[0],
                     "cgi_cand": cgi_cand,
                     "self.repo_info": self.repo_info,
                     "returned": True})
        return True

    def translate_path(self, path):
        """This extension simply ensures that the curdir is docroot, which is
        assumed by the base method.
        """
        # XXX unclear whether this block is a bug. Assuming it was added for a
        # reason. While ``os.chdir()`` can result in a ``FileNotFoundError``,
        # and chdir(3) lists some ``ENO*`` errors on its man page, getcwd(3)
        # does not.
        try:
            thisdir = os.getcwd()
        except FileNotFoundError:
            thisdir = DOCROOT
            self.dlog("translate_path - call to os.getcwd() failed")
        os.chdir(self.docroot)
        outpath = super(HTTPBackendHandler, self).translate_path(path)
        os.chdir(thisdir)
        return outpath

    def is_repo(self, abspath):
        if (os.path.isfile(os.path.join(abspath, 'HEAD')) or
                os.path.isdir(os.path.join(abspath, 'refs/heads'))):
            return True
        return False

    def find_repo(self, lhs, rhs=None, allow_fake=False):
        """Strip leading components from rhs and append to lhs until a
        valid Git repo is encountered.

        TODO this is impossible to follow; use lists instead of strings
        """
        if rhs is None:
            rhs = lhs
            lhs = "/"
        fakes = ""
        path = lhs.rstrip('/') + '/' + rhs.lstrip('/')
        i = path.find('/', len(lhs)+1)
        while i >= 0:
            nextlhs = path[:i]
            nextrhs = path[i+1:]
            candidate = self.translate_path(nextlhs)
            if allow_fake and not os.path.isdir(candidate):
                # Roll back path
                path = lhs + path[i:]
                nextlhs = lhs
                candidate, nextfake = os.path.split(candidate)
                fakes += "/" + nextfake
            if os.path.isdir(candidate) and not self.is_repo(candidate):
                lhs, rhs = nextlhs, nextrhs
                i = path.find('/', len(lhs)+1)
            else:
                break
        # XXX previously, some call sites expected a leading slash on the
        # second item. Not sure if all have been updated.
        return (lhs + (fakes.lstrip("/") if lhs.endswith("/") else fakes),
                rhs.lstrip("/"))

    def verify_pass(self, saved, received):
        """This attempts to compare a saved hash from the .htpasswd
        file to the sent password. The only supported formats are unix
        crypt(3) and sha1. Both args must be strings.
        """
        if saved.startswith("$apr1") and self.has_openssl is True:
            salt = saved.split("$")[2]
            try:
                checked = check_output("openssl passwd -apr1 -salt".split() +
                                       [salt, received])
            except CalledProcessError:
                self.has_openssl = False
            else:
                if checked.decode().strip() == saved:
                    return True
        elif saved.startswith("{SHA}"):
            import base64
            import hashlib
            binpass = saved.partition("{SHA}")[-1].encode()
            binpass = base64.decodebytes(binpass)
            if hashlib.sha1(received.encode()).digest() == binpass:
                return True
        elif len(saved) == 13:
            import crypt
            if crypt.crypt(received, saved[:2]) == saved:
                return True
        return False

    def send_head(self):
        """This version delegates to the original when (1) authorization
        doesn't apply to a particular path or (2) credentials check out.
        Otherwise, it responds with UNAUTHORIZED or FORBIDDEN. The
        original passes GET requests to the SimpleHTTPRequestHandler,
        which requires a trailing slash for dirs below DOCROOT, if an
        html directory listing is to be generated and returned.
        Otherwise, it responds with a 301 MOVED_PERMANENTLY.
        """
        if not self.auth_dict:
            return super(HTTPBackendHandler, self).send_head()
        #
        # self.dlog("send_head - auth_dict", **self.auth_dict)
        collapsed_path = _url_collapse_path(self.path)
        is_protected = False
        # XXX iter var name too long, need below
        for restricted_path in self.auth_dict:
            if (collapsed_path.startswith(restricted_path.rstrip("/") + "/") or
                    collapsed_path == restricted_path.rstrip("/")):
                is_protected = True
                break
        # This is just the entry for the path; unrelated to "description" field
        realm_info = self.auth_dict[restricted_path]
        privaterepo = realm_info.get('privaterepo', False)
        if (is_protected is False or privaterepo is False and
                "service=git-receive-pack" not in collapsed_path):
            return super(HTTPBackendHandler, self).send_head()
        description = realm_info.get('description', "Basic auth requested")
        # XXX - this option is currently bunk, although it does trigger the
        # exporting of REMOTE_USER below, which the git exes seem to ignore.
        # If implementing, it would most likely be limited to unix systems
        # with read access to /etc/passwd and /etc/group. The actual modified
        # files would still end up being owned by the server process UID.
        realaccount = realm_info.get('realaccount', REQURE_ACCOUNT)
        try:
            secretsfile = realm_info.get('secretsfile')
            with open(secretsfile) as f:
                secretlines = f.readlines()
        except TypeError:
            self.send_error(HTTPStatus.EXPECTATION_FAILED,
                            "Could not read .htpasswd file")
            return None
        else:
            secdict = {}
            for line in secretlines:
                if ':' not in line:
                    continue
                u, p = line.split(":")
                if p.startswith("$apr1") and self.has_openssl is None:
                    try:
                        check_output(("openssl", "version"))
                    except (FileNotFoundError, CalledProcessError):
                        self.log_error("send_head - Apache md5 support needed"
                                       " but not found. See usage note.")
                        self.has_openssl = False
                        continue
                    else:
                        self.has_openssl = True
                elif p.startswith("$2y"):
                    # Placeholder for passlib integration
                    self.log_error("send_head - bcrypt support requested but "
                                   "not found. See usage note.")
                    continue
                secdict.update({u.strip(): p.strip()})
            del line, u, p
            # self.dlog("send_head - secdict", **secdict)
        authorization = self.headers.get("authorization")
        #
        if authorization:
            self.dlog("send_head - auth string sent: %r" % authorization)
            authorization = authorization.split()
            if len(authorization) == 2:
                import base64
                import binascii
                os.environ.update(AUTH_TYPE=authorization[0])
                if authorization[0].lower() != "basic":
                    self.send_error(HTTPStatus.NOT_ACCEPTABLE,
                                    "Auth type %r not supported!" %
                                    authorization[0])
                    return None
                #
                try:
                    authorization = authorization[1].encode('ascii')
                    authorization = base64.decodebytes(
                                    authorization).decode('ascii')
                except (binascii.Error, UnicodeError):
                    pass
                else:
                    authorization = authorization.split(':')
                    self.dlog("send_head - processed auth: "
                              "{!r}".format(authorization))
                    if (len(authorization) == 2 and
                            authorization[0] in secdict and
                            self.verify_pass(secdict[authorization[0]],
                                             authorization[1])):
                        if realaccount:
                            os.environ.update(REMOTE_USER=authorization[0])
                        return super(HTTPBackendHandler, self).send_head()
                    else:
                        self.send_error(HTTPStatus.FORBIDDEN,
                                        "Problem authenticating "
                                        "{!r}".format(authorization[0]))
                        return None
            #
            # Auth string had > 1 space or exception was raised
            self.send_error(HTTPStatus.UNPROCESSABLE_ENTITY,
                            "Problem reading authorization: "
                            "{!r}".format(authorization[0]))
            return None
        else:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate",
                             'Basic realm="%s"' % description)
            self.end_headers()
            return None

    def run_cgi(self):
        """
        Execute a CGI script
        --------------------
        -   The leading slash on ``rest`` is illogical, but it's a
            longstanding CGI convention, like the "path" element.
        - ``git_root`` merely means the URI of the dir containing the
            Git repo. It can have any number of leading components and
            must exist (it cannot be ``/``).
        - ``$DOCROOT`` is a system folder, like ``/var/www``, that's
            mapped to the root URI ``/``.::

            IDENT DESC                       EXAMPLE
            ----- -------------------------  -----------------------
            root  git_root URI               /subdir/repo_parent
            rest  stuff below repo           /refs/heads/..
            repo  unadorned, lone repo name  myrepo.git
            uri   repo URI                   /git_root/myrepo.git
            abs   real abs path to git_root  /var/www/git_root

        GnuTLS issue
        ------------
        In Debian (and probably Ubuntu), both curl and git are built
        against GnuTLS, which raises error -110: "the TLS connection was
        non-properly terminated." So, either http-backend isn't sending
        ``Content-Length`` or it's getting lost somewhere. Looking at
        the source_, we see that ``get_info_refs`` does indeed send
        it when the "service" param isn't present in the query string,
        which isn't the case with everyday requests like fetches and
        pushes.

        Also Tried messing with the ``Connection: close`` header field,
        but that didn't seem to do anything (or I wasn't reading the spec_
        correctly).

        .. _source: https://github.com/git/git/http-backend.c
        .. _spec: https://tools.ietf.org/html/rfc2616#section-14.10
        """
        root, rest, namespace = self.repo_info
        # Find an explicit query string, if present.
        rest, _, query = rest.partition('?')
        # Shift all path components preceding ``repo`` in ``rest`` to ``root``
        root, rest = self.find_repo(root, rest)
        #
        # Extract part after repo name and make candidate for ``plumbing_cmd``.
        # Guranteed to remove 1st component when depth > 1. This gets tacked on
        # to ``$PATH_INFO.``
        i = rest.find('/')
        if i >= 0:
            repo, rest = rest[:i], rest[i:]
        else:
            repo, rest = rest, ''
        repo_uri = root + '/' + repo
        repo_abs = self.translate_path(repo_uri)
        #
        self.dlog("run_cgi - vars", **dict(
            item for item in locals().items() if item[0] in
            ('root', 'rest') or item[0].startswith('repo')))
        #
        if ENFORCE_DOTGIT is True and not repo.endswith('.git'):
            self.send_error(HTTPStatus.FORBIDDEN,
                            "- invalid repo name %r" % repo)
            return
        if any(p in os.listdir(self.git_exec_path) for
               p in (rest.strip('/'), query.rsplit('=')[-1])):
            plumbing_cmd = os.path.join(self.git_exec_path,
                                        query.rsplit('=')[-1] if
                                        query else rest.strip('/'))
            if not self.is_executable(plumbing_cmd):
                # XXX this status code is only meant for header items
                self.send_error(
                    HTTPStatus.PRECONDITION_FAILED,
                    "CGI script is not executable (%r)" % plumbing_cmd)
                return
            self.dlog("run_cgi - git-plumbing command:", path=plumbing_cmd)
        else:
            self.send_error(HTTPStatus.NOT_FOUND,
                            "- CGI Script '%s' not found." % repo_abs)
            return
        #
        # Env vars only contain strings, so a simple ``env = dict(os.environ)``
        # would do the same thing, no? Whatever, go with upstream...
        env = copy.deepcopy(os.environ)
        uqrest = urlparse.unquote(rest)
        #
        # As required by git-http-backend(1); These never change.
        env["GIT_HTTP_EXPORT_ALL"] = ""
        # Absolute path to ``git_root`` (dir above Git repo)
        env["GIT_PROJECT_ROOT"] = os.path.abspath(
            os.path.join(self.docroot, root.lstrip('/')))
        if namespace is not None:
            env["GIT_NAMESPACE"] = namespace
        #
        # Reformat env vars based on incoming request syntax
        gitprg_path = os.path.join(self.git_exec_path, 'git-http-backend')
        env['SCRIPT_NAME'] = "git-http-backend"
        #
        env['PATH_INFO'] = '/' + repo.lstrip("/") + uqrest
        # This is used by git-http-backend when ``GIT_PROJECT_ROOT`` is unset
        env['PATH_TRANSLATED'] = self.translate_path(os.path.join(*(
            c.strip('/') for c in (root, repo, uqrest))))
        #
        env['QUERY_STRING'] = query
        #
        # XXX was perviously assumed that, only ``git-receive-pack`` required
        # REMOTE_USER, but this might not be true. Not sure whether this is
        # handled by the remote git-exec program or the os or the server.
        if any("receive-pack" in var for var in (query, rest)):
            env.setdefault("REMOTE_USER", os.getenv("USER"))
        #
        if not os.path.isfile(os.path.join(repo_abs, 'HEAD')):
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "%s says: not a Git repo (%r)" % (sys.argv[0], repo_uri))
            return
        # Reference: http://hoohoo.ncsa.uiuc.edu/cgi/env.html
        # XXX Much of the following could be prepared ahead of time!
        env['SERVER_SOFTWARE'] = self.version_string()
        env['SERVER_NAME'] = self.server.server_name
        env['GATEWAY_INTERFACE'] = 'CGI/1.1'
        env['SERVER_PROTOCOL'] = self.protocol_version
        env['SERVER_PORT'] = str(self.server.server_port)
        env['REQUEST_METHOD'] = self.command
        env['REMOTE_ADDR'] = self.client_address[0]
        if hasattr(self.headers, "get_content_type"):
            env['CONTENT_TYPE'] = self.headers.get(
                'content-type', self.headers.get_content_type()
            )
        else:
            env['CONTENT_TYPE'] = (self.headers.typeheader or
                                   self.headers.type)
        length = self.headers.get('content-length')
        if length:
            env['CONTENT_LENGTH'] = length
        referer = self.headers.get('referer')
        if referer:
            env['HTTP_REFERER'] = referer
        accept = []
        for line in self.headers.getallmatchingheaders('accept'):
            if line[:1] in "\t\n\r ":
                accept.append(line.strip())
            else:
                accept = accept + line[7:].split(',')
        env['HTTP_ACCEPT'] = ','.join(accept)
        ua = self.headers.get('user-agent')
        if ua:
            env['HTTP_USER_AGENT'] = ua
        if hasattr(self.headers, "get_all"):
            co = filter(None, self.headers.get_all('cookie', []))
        else:
            co = filter(None, self.headers.getheaders('cookie'))
        cookie_str = ', '.join(co)
        if cookie_str:
            env['HTTP_COOKIE'] = cookie_str
        #
        self.dlog("self.headers:", **self.headers)
        #
        # XXX Other HTTP_* headers
        # Since we're setting the env in the parent, provide empty
        # values to override previously set values
        rfcvars = ('QUERY_STRING', 'REMOTE_HOST', 'CONTENT_LENGTH',
                   'HTTP_USER_AGENT', 'HTTP_COOKIE', 'HTTP_REFERER')
        for k in rfcvars:
            env.setdefault(k, "")
        #
        # Env vars required by ``git-http-backend`` and/or rfc3875
        self.dlog("run_cgi - envvars",
                  **dict(item for item in env.items() if
                         item[0] in rfcvars or
                         any(item[0].startswith(pre + '_') for
                             pre in 'query path git remote'.upper().split())))
        #
        self.send_response(HTTPStatus.OK, "Script output follows")
        if hasattr(self, "flush_headers"):
            self.flush_headers()
        # decoded_query = query.replace('+', ' ')
        cmdline = [gitprg_path]
        if query and '=' not in query:
            cmdline.append(query)
        self.dlog("command: %s", list2cmdline(cmdline))
        try:
            nbytes = int(length)
        except (TypeError, ValueError):
            nbytes = 0
        p = Popen(cmdline, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)
        if self.command.lower() == "post" and nbytes > 0:
            data = self.rfile.read(nbytes)
        else:
            data = None
        # throw away additional data [see bug #427345]
        while select.select([self.rfile._sock], [], [], 0)[0]:
            if not self.rfile._sock.recv(1):
                break
        stdout, stderr = p.communicate(data)
        # See note in docstring re GnuTLS and Content-Length
        hdr, _, payload = stdout.partition(b"\r\n\r\n")
        if b"Content-Length" in hdr:
            self.log_error("'Content-Length' already present!: %r", hdr)
        length = len(payload)
        self.send_header("Content-Length", length)
        if hasattr(self, "flush_headers"):
            self.flush_headers()
        #
        self.wfile.write(stdout)
        if stderr:
            self.log_error('%s', stderr)
        p.stderr.close()
        p.stdout.close()
        status = p.returncode
        if status:
            self.log_error("CGI script exit status %#x", status)
        else:
            self.dlog("CGI script exited OK")


def validate_logpath(inpath=None, create=False, maxsize=2):
    import os
    outpath = None
    if inpath is not None:
        # On UNIX, write permissions of parent dir don't matter for
        # existing files
        if os.path.isfile(inpath) and os.access(inpath, os.W_OK):
            outpath = inpath
        elif os.access(os.path.dirname(inpath), os.W_OK):
            outpath = inpath
            if os.path.isfile(outpath):
                outpath += ".new"
            os.mknod(outpath, mode=0o644)
        else:
            return None
        # If attempting to support Windows, assume Python version >= 3.5
        if os.path.getsize(outpath) > 2**20 * maxsize:
            os.truncate(outpath, 0)
    return outpath


def set_ssl_context():
    """
    Verify local certificate paths and return an ssl context object.
    """
    global CERTFILE, KEYFILE, DHPARAMS, PORT
    gdict = globals()
    for fid in ("CERTFILE", "KEYFILE", "DHPARAMS"):
        fpath = gdict.get(fid)
        if fpath is None:
            continue
        fpath = os.path.expanduser(fpath)
        fpath = os.path.expandvars(fpath)
        if os.path.isfile(fpath):
            gdict[fid] = os.path.realpath(fpath)
        else:
            gdict[fid] = None
    #
    if CERTFILE is None:
        KEYFILE = DHPARAMS = None
        return None
    # Ensure lone certificates include a key
    if KEYFILE is None:
        with open(CERTFILE) as f:
            pem = f.readlines()
            msg = None
            try:
                k = next(pem.index(l) for l in pem if 'END PRIVATE' in l)
                c = next(pem.index(l) for l in pem if 'CERTIFICATE' in l)
            except StopIteration:
                msg = ("Invalid certificate. Please set ``*_KEYFILE`` or "
                       "provide a combined cert in PEM format.")
            else:
                if not k < c:
                    msg = ("Invalid certificate. For combined PEM certs, "
                           "the key must appear atop the cert")
            if msg:
                raise ValueError(msg)
    import ssl
    context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
    # Like ``SSLContext.set_default_verify_paths()``, ``set_ecdh_curve()``
    # doesn't exist in 3.5.x, at least not on Fedora's system python3.
    # XXX some expert tutorial from 2013 says to set this to ``secp384r1``
    # on Apache/NGINX. Not sure if that's still the way to go.
    if DHPARAMS:
        context.load_dh_params(DHPARAMS)
    # XXX probably best not to mess with these...
    # context.set_ciphers("EECDH+AESGCM:EDH+AESGCM:AES256+EECDH:AES256+EDH")
    context.load_cert_chain(CERTFILE, KEYFILE)
    if PORT == 8000:
        PORT = 4443
    return context


def register_signals(server, quitters, keepers=None):
    """Attach a handler to signals named in quitters or keepers.
    The module's default behavior is to quit without teardown for
    certain "unknown" signals like USR1.
    """
    quitters = (s.upper() if s.upper().startswith("SIG") else
                "SIG" + s.upper() for s in quitters)
    if keepers is not None:
        keepers = [s.upper() if s.upper().startswith("SIG") else
                   "SIG" + s.upper() for s in keepers]
    else:
        keepers = ()
    # This syntax is forbidden in Python 2.7: ``set((*quitters, *keepers))``
    signames = set(quitters) | set(keepers)
    import signal
    # Can also ``filter(None, Iterator)`` to get rid of falsey items
    numxsig = {getattr(signal, sig, None): sig for sig in signames if
               sig in dir(signal)}

    def handle_stay_signal(signo, frame):
        print("\nReceived {!r} from controlling terminal; "
              "ignoring...".format(numxsig[signo]),
              file=sys.stderr)
        return 0

    def handle_quit_signal(signo, frame):
        # This just calls ``socket.close()`` (rather than shutdown)
        server.server_close()
        msg = "\nReceived %r, {} server, quitting..." % numxsig[signo]
        if hasattr(server.socket, "_closed"):
            print(msg.format("successfully closed" if server.socket._closed
                             else "FAILED TO CLOSE"), file=sys.stderr)
        else:
            print(msg.format("successfully closed" if "closedsocket" in
                             repr(server.socket._sock) else "FAILED TO CLOSE"),
                  file=sys.stderr)
        sys.exit(0)

    for num, name in numxsig.items():
        # Special case for job control signals propagated after a Ctrl-Z.
        if name in keepers:
            signal.signal(num, handle_stay_signal)
        else:
            signal.signal(num, handle_quit_signal)


def serve(server_class, name="Git services", context=None):
    """This is basically just ``__main__`` from ``http.server``
    """
    from time import strftime
    #
    server = server_class((HOST, PORT), HTTPBackendHandler, context)
    #
    register_signals(server, ("TERM", "HUP", "INT"), ("TSTP", "TTOU", "TTIN"))
    #
    # Copy fmt from ``BaseHTTPRequestHandler.log_message``
    bookend_fmt = "{0} - - [{2}] {3} serving %s on {0} over port {1}" % name
    time_fmt = "%d/%b/%Y %H:%M:%S"
    #
    host, port = server.socket.getsockname()
    print(bookend_fmt.format(host, port, strftime(time_fmt), "Started"),
          file=sys.stderr)
    print("{} - - [{}] PID: {}, PPID: {}".format(
        host, strftime(time_fmt), os.getpid(), os.getppid()), file=sys.stderr)
    if not LOGFILE:
        print("\n{}\n".format("Hit Ctrl-C to exit."), file=sys.stderr)
    sys.stderr.flush()
    #
    try:
        server.serve_forever()
    finally:
        print("\n" + bookend_fmt.format(host, port, strftime(time_fmt),
                                        "Stopped"), file=sys.stderr)
        server.server_close()


def main():
    #
    if sys.version_info < (3, 5) and list(sys.version_info)[:2] != [2, 7]:
        print("WARNING: untested on Python versions < 3.5, except for 2.7",
              file=sys.stderr)
    #
    if len(sys.argv) > 1 and sys.argv[1].lstrip("-") in ("help", "h"):
        print("\n".join(l[4:] for l in
                        __doc__.split("Notes\n")[0].splitlines() if
                        not l.endswith("::")))
        return
    #
    logfile = validate_logpath(LOGFILE, create=True, maxsize=0)
    #
    if logfile is not None:

        class AsRequested(CtxServer):
            def service_actions(self):
                sys.stderr.flush()
                sys.stdout.flush()

        from contextlib import redirect_stderr, redirect_stdout
        with open(logfile, 'a') as f:
            with redirect_stderr(f):
                with redirect_stdout(sys.stderr):
                    serve(AsRequested, context=set_ssl_context())
    else:
        serve(CtxServer, context=set_ssl_context())


if __name__ == "__main__":
    sys.exit(main())


# Copyright 2016 Jane Soko <boynamedjane@misled.ml>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
