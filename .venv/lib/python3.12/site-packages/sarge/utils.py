# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2019 Vinay M. Sajip. See LICENSE for licensing information.
#
# sarge: Subprocess Allegedly Rewards Good Encapsulation :-)
#
import os
import re
import sys

try:
    from shutil import which
except ImportError:
    # Copied from Python 3.3.
    def which(cmd, mode=os.F_OK | os.X_OK, path=None):
        """Given a command, mode, and a PATH string, return the path which
        conforms to the given mode on the PATH, or None if there is no such
        file.

        `mode` defaults to os.F_OK | os.X_OK. `path` defaults to the result
        of os.environ.get("PATH"), or can be overridden with a custom search
        path.

        """
        # Check that a given file can be accessed with the correct mode.
        # Additionally check that `file` is not a directory, as on Windows
        # directories pass the os.access check.
        def _access_check(fn, mode):
            return (os.path.exists(fn) and os.access(fn, mode)
                    and not os.path.isdir(fn))

        # If we're given a path with a directory part, look it up directly rather
        # than referring to PATH directories. This includes checking relative to the
        # current directory, e.g. ./script
        if os.path.dirname(cmd):
            if _access_check(cmd, mode):
                return cmd
            return None

        if path is None:
            path = os.environ.get("PATH", os.defpath)
        if not path:
            return None
        path = path.split(os.pathsep)

        if sys.platform == "win32":
            # The current directory takes precedence on Windows.
            if not os.curdir in path:
                path.insert(0, os.curdir)

            # PATHEXT is necessary to check on Windows.
            pathext = os.environ.get("PATHEXT", "").split(os.pathsep)
            # See if the given file matches any of the expected path extensions.
            # This will allow us to short circuit when given "python.exe".
            # If it does match, only test that one, otherwise we have to try
            # others.
            if any(cmd.lower().endswith(ext.lower()) for ext in pathext):
                files = [cmd]
            else:
                files = [cmd + ext for ext in pathext]
        else:
            # On other platforms you don't have things like PATHEXT to tell you
            # what file suffixes are executable, so just pass on cmd as-is.
            files = [cmd]

        seen = set()
        for dir in path:
            normdir = os.path.normcase(dir)
            if not normdir in seen:
                seen.add(normdir)
                for thefile in files:
                    name = os.path.join(dir, thefile)
                    if _access_check(name, mode):
                        return name
        return None

if sys.platform == 'win32':
    try:
        import winreg
    except ImportError:
        import _winreg as winreg

    COMMAND_RE = re.compile(r'^"([^"]*)" "%1" %\*$')

    def find_command(cmd):
        """
        Convert a command into a script name, if possible, and find
        any associated executable.
        :param cmd: The command (e.g. 'hello')
        :returns: A 2-tuple. The first element is an executable associated
                  with the extension of the command script. The second
                  element is the script name, including the extension and
                  pathname if found on the path. Example for 'hello' might be
                  (r'c:\Python\python.exe', r'c:\MyTools\hello.py').
        """
        result = None
        cmd = which(cmd)
        if cmd:
            if cmd.startswith('.\\'):
                cmd = cmd[2:]
            _, extn = os.path.splitext(cmd)
            HKCR = winreg.HKEY_CLASSES_ROOT
            try:
                ftype = winreg.QueryValue(HKCR, extn)
                path = os.path.join(ftype, 'shell', 'open', 'command')
                s = winreg.QueryValue(HKCR, path)
                exe = None
                m = COMMAND_RE.match(s)
                if m:
                    exe = m.groups()[0]
                    result = exe, cmd
            except OSError:
                pass
        return result
