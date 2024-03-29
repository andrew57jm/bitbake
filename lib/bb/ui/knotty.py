#
# BitBake (No)TTY UI Implementation
#
# Handling output to TTYs or files (no TTY)
#
# Copyright (C) 2006-2012 Richard Purdie
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

from __future__ import division

import os
import sys
import select
import xmlrpclib
import logging
import progressbar
import signal
import bb.msg
import time
import fcntl
import struct
import copy
import atexit
from bb.ui import uihelper
from bb.ui.crumbs.multilogtail import MultiLogTail

featureSet = [bb.cooker.CookerFeatures.SEND_SANITYEVENTS]

logger = logging.getLogger("BitBake")
interactive = sys.stdout.isatty()

class BBProgress(progressbar.ProgressBar):
    def __init__(self, msg, maxval):
        self.msg = msg
        widgets = [progressbar.Percentage(), ' ', progressbar.Bar(), ' ',
           progressbar.ETA()]

        try:
            self._resize_default = signal.getsignal(signal.SIGWINCH)
        except:
            self._resize_default = None
        progressbar.ProgressBar.__init__(self, maxval, [self.msg + ": "] + widgets, fd=sys.stdout)

    def _handle_resize(self, signum, frame):
        progressbar.ProgressBar._handle_resize(self, signum, frame)
        if self._resize_default:
            self._resize_default(signum, frame)
    def finish(self):
        progressbar.ProgressBar.finish(self)
        if self._resize_default:
            signal.signal(signal.SIGWINCH, self._resize_default)

class NonInteractiveProgress(object):
    fobj = sys.stdout

    def __init__(self, msg, maxval):
        self.msg = msg
        self.maxval = maxval

    def start(self):
        self.fobj.write("%s..." % self.msg)
        self.fobj.flush()
        return self

    def update(self, value):
        pass

    def finish(self):
        self.fobj.write("done.\n")
        self.fobj.flush()

def new_progress(msg, maxval):
    if interactive:
        return BBProgress(msg, maxval)
    else:
        return NonInteractiveProgress(msg, maxval)

def pluralise(singular, plural, qty):
    if(qty == 1):
        return singular % qty
    else:
        return plural % qty


class InteractConsoleLogFilter(logging.Filter):
    def __init__(self, tf, format):
        self.tf = tf
        self.format = format

    def filter(self, record):
        if self.tf.filterOn and record.levelno == self.format.NOTE and (record.msg.startswith("Running") or record.msg.startswith("recipe ")):
            return False
        self.tf.clearFooter()
        return True

class TerminalFilter(object):
    columns = 80

    def sigwinch_handle(self, signum, frame):
        self.columns = self.getTerminalColumns()
        if self._sigwinch_default:
            self._sigwinch_default(signum, frame)

    def getTerminalColumns(self):
        def ioctl_GWINSZ(fd):
            try:
                cr = struct.unpack('hh', fcntl.ioctl(fd, self.termios.TIOCGWINSZ, '1234'))
            except:
                return None
            return cr
        cr = ioctl_GWINSZ(sys.stdout.fileno())
        if not cr:
            try:
                fd = os.open(os.ctermid(), os.O_RDONLY)
                cr = ioctl_GWINSZ(fd)
                os.close(fd)
            except:
                pass
        if not cr:
            try:
                cr = (env['LINES'], env['COLUMNS'])
            except:
                cr = (25, 80)
        return cr[1]

    def __init__(self, main, helper, console, errconsole, format):
        self.main = main
        self.helper = helper
        self.cuu = None
        self.topMode = False
        self.filterOn = False
        self.stdinbackup = None
        self.interactive = sys.stdout.isatty()
        self.footer_present = False
        self.lastpids = []

        if not self.interactive:
            return

        try:
            import curses
        except ImportError:
            sys.exit("FATAL: The knotty ui could not load the required curses python module.")

        import termios
        self.curses = curses
        self.termios = termios
        try:
            fd = sys.stdin.fileno()
            self.stdinbackup = termios.tcgetattr(fd)
            new = copy.deepcopy(self.stdinbackup)
            new[3] = new[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            curses.setupterm()
            if curses.tigetnum("colors") > 2:
                format.enable_color()
            self.ed = curses.tigetstr("ed")
            if self.ed:
                self.cuu = curses.tigetstr("cuu")
            try:
                self._sigwinch_default = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, self.sigwinch_handle)
            except:
                pass
            self.columns = self.getTerminalColumns()
        except:
            self.cuu = None
        self.topMode = True
        self.filterOn = True
        console.addFilter(InteractConsoleLogFilter(self, format))
        errconsole.addFilter(InteractConsoleLogFilter(self, format))

    def setTopMode(self):
        if self.topMode:
            return
        self.topMode = True
        self.setFilterOn()

    def setNormalMode(self):
        if not self.topMode:
            return
        self.topMode = False
        self.setFilterOff()

    def setFilterOn(self):
        self.filterOn = True

    def setFilterOff(self):
        self.filterOn = False

    def clearFooter(self):
        if not self.topMode:
            return
        if self.footer_present:
            lines = self.footer_present
            sys.stdout.write(self.curses.tparm(self.cuu, lines))
            sys.stdout.write(self.curses.tparm(self.ed))
        self.footer_present = False

    def updateFooterForce(self):
        self.footer_present = False
        self.updateFooter()

    def updateFooter(self):
        if not self.cuu or not self.topMode:
            return
        activetasks = self.helper.running_tasks
        failedtasks = self.helper.failed_tasks
        runningpids = self.helper.running_pids
        if self.footer_present and (self.lastcount == self.helper.tasknumber_current) and (self.lastpids == runningpids):
            return
        if self.footer_present:
            self.clearFooter()
        if (not self.helper.tasknumber_total or self.helper.tasknumber_current == self.helper.tasknumber_total) and not len(activetasks):
            return
        tasks = []
        for t in runningpids:
            tasks.append("%s (pid %s)" % (activetasks[t]["title"], t))

        if self.main.shutdown:
            content = "Waiting for %s running tasks to finish:" % len(activetasks)
        elif not len(activetasks):
            content = "No currently running tasks (%s of %s)" % (self.helper.tasknumber_current, self.helper.tasknumber_total)
        else:
            content = "Currently %s running tasks (%s of %s):" % (len(activetasks), self.helper.tasknumber_current, self.helper.tasknumber_total)
        print(content)
        lines = 1 + int(len(content) / (self.columns + 1))
        for tasknum, task in enumerate(tasks):
            content = "%s: %s" % (tasknum, task)
            print(content)
            lines = lines + 1 + int(len(content) / (self.columns + 1))
        self.footer_present = lines
        self.lastpids = runningpids[:]
        self.lastcount = self.helper.tasknumber_current

    def finish(self):
        if self.stdinbackup:
            fd = sys.stdin.fileno()
            self.termios.tcsetattr(fd, self.termios.TCSADRAIN, self.stdinbackup)

class StdinMgr:
    def __init__(self):
        import termios
        self.termios = termios
        self.stdinbackup = None
        self.fd = None
        if sys.stdin.isatty():
            self.fd = sys.stdin.fileno()
            self.stdinbackup = self.termios.tcgetattr(self.fd)
            new = self.termios.tcgetattr(self.fd)
            new[3] = new[3] & ~self.termios.ICANON & ~self.termios.ECHO
            self.termios.tcsetattr(self.fd, self.termios.TCSANOW, new)

    def poll(self):
        if not self.stdinbackup:
            return False
        try:
            ret = select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])
        except:
            return False
        return ret

    def restore(self):
        if self.stdinbackup:
            self.termios.tcsetattr(self.fd, self.termios.TCSADRAIN,
                                   self.stdinbackup)
            self.stdinbackup = None
        # Force echo back on "just in case" something went haywire with an exception
        if sys.stdin.isatty():
            new = self.termios.tcgetattr(self.fd)
            new[3] = new[3] | self.termios.ECHO
            self.termios.tcsetattr(self.fd, self.termios.TCSANOW, new)

class RtLogLevel:
    def __init__(self, handler, logfilter, mlt, tf):
        self.displaytail = False
        self.displayLogLocations = False
        self.handler = handler
        self.logfilter = logfilter
        self.defaultLevel = logfilter.getFiltLevel()
        self.mlt = mlt
        self.tf = tf

    def displayLogs(self):
        if self.displaytail:
            self.mlt.displayLogs()

    def setLevel(self, input, verbose):
        if input == "1" or input == "0":
            if verbose:
                print "NOTE: Turning off real time log tail"
            self.logfilter.setFiltLevel(self.handler, self.defaultLevel)
            self.displaytail = False
            if isinstance(self.tf, TerminalFilter):
                if input == "0":
                    self.tf.setNormalMode()
                else:
                    self.tf.setTopMode()
        elif input == "2":
            if verbose:
                print "NOTE: Turning on real time log tail"
            self.logfilter.setFiltLevel(self.handler, self.defaultLevel)
            self.displaytail = True
        elif input == "3":
            if verbose:
                print "NOTE: Turning on DEBUG logging"
            self.logfilter.setFiltLevel(self.handler, logging.DEBUG)
            self.displaytail = False
        elif input == "4":
            if verbose:
                print "NOTE: Turning on DEBUG logging + real time log tail"
            self.logfilter.setFiltLevel(self.handler, logging.DEBUG)
            self.displaytail = True
        elif input == "t":
            if verbose:
                print "NOTE: Activing task \"top\" mode"
            if isinstance(self.tf, TerminalFilter):
                self.tf.setTopMode()
        elif input == "N":
            if verbose:
                print "NOTE: Turning on task notes"
            if isinstance(self.tf, TerminalFilter):
                self.tf.setFilterOff()
        elif input == "l":
            if verbose:
                print "NOTE: Activating log locations display"
            self.displayLogLocations = True
        elif input == "L":
            if verbose:
                print "NOTE: Disable log locations display"
            self.displayLogLocations = False
        elif input == "h" or input == "?":
            print "============================================="
            print "Interaction help commands:"
            print " 0 - Linear logging"
            print " 1 - turn off real time log tail"
            print " 2 - turn on real time log tail"
            print " 3 - turn on debug logging"
            print " 4 - turn on debug logging and real time log tail"
            print " l - emit log locations (L to turn off)"
            print " t - Display tasks in \"top\" mode"
            print " N - Display all runtime NOTE's that are normally filtered (0 or 1 toggles off)"
            print " h - display commands"
            return False
        return True

def _log_settings_from_server(server):
    # Get values of variables which control our output
    includelogs, error = server.runCommand(["getVariable", "BBINCLUDELOGS"])
    if error:
        logger.error("Unable to get the value of BBINCLUDELOGS variable: %s" % error)
        raise BaseException(error)
    loglines, error = server.runCommand(["getVariable", "BBINCLUDELOGS_LINES"])
    if error:
        logger.error("Unable to get the value of BBINCLUDELOGS_LINES variable: %s" % error)
        raise BaseException(error)
    consolelogfile, error = server.runCommand(["getVariable", "BB_CONSOLELOG"])
    if error:
        logger.error("Unable to get the value of BB_CONSOLELOG variable: %s" % error)
        raise BaseException(error)
    bb_rt_loglevel, error = server.runCommand(["getVariable", "BB_RT_LOGLEVEL"])
    if error:
        logger.error("Unable to get the value of BB_RT_LOGLEVEL variable: %s" % error)
        raise BaseException(error)
    return includelogs, loglines, consolelogfile, bb_rt_loglevel

_evt_list = [ "bb.runqueue.runQueueExitWait", "bb.event.LogExecTTY", "logging.LogRecord",
              "bb.build.TaskFailed", "bb.build.TaskBase", "bb.event.ParseStarted",
              "bb.event.ParseProgress", "bb.event.ParseCompleted", "bb.event.CacheLoadStarted",
              "bb.event.CacheLoadProgress", "bb.event.CacheLoadCompleted", "bb.command.CommandFailed",
              "bb.command.CommandExit", "bb.command.CommandCompleted",  "bb.cooker.CookerExit",
              "bb.event.MultipleProviders", "bb.event.NoProvider", "bb.runqueue.sceneQueueTaskStarted",
              "bb.runqueue.runQueueTaskStarted", "bb.runqueue.runQueueTaskFailed", "bb.runqueue.sceneQueueTaskFailed",
              "bb.event.BuildBase", "bb.build.TaskStarted", "bb.build.TaskSucceeded", "bb.build.TaskFailedSilent"]

def main(server, eventHandler, params, tf = TerminalFilter):

    includelogs, loglines, consolelogfile, bb_rt_loglevel = _log_settings_from_server(server)

    if sys.stdin.isatty() and sys.stdout.isatty():
        log_exec_tty = True
    else:
        log_exec_tty = False

    # MultiTail affected varialbles
    numthreads = server.runCommand(["getVariable", "BB_NUMBER_THREADS"])
    if numthreads is None or numthreads == "1":
        mlt = MultiLogTail(False)
    else:
        mlt = MultiLogTail(True)

    helper = uihelper.BBUIHelper()

    console = logging.StreamHandler(sys.stdout)
    errconsole = logging.StreamHandler(sys.stderr)
    format_str = "%(levelname)s: %(message)s"
    format = bb.msg.BBLogFormatter(format_str)
    bb.msg.addDefaultlogFilter(console, bb.msg.BBLogFilterStdOut)
    bb.msg.addDefaultlogFilter(errconsole, bb.msg.BBLogFilterStdErr)
    logfilter = bb.msg.addDefaultlogFilter(console)
    console.setFormatter(format)
    errconsole.setFormatter(format)
    logger.addHandler(console)
    logger.addHandler(errconsole)

    if params.options.remote_server and params.options.kill_server:
        server.terminateServer()
        return

    if consolelogfile and not params.options.show_environment:
        bb.utils.mkdirhier(os.path.dirname(consolelogfile))
        conlogformat = bb.msg.BBLogFormatter(format_str)
        consolelog = logging.FileHandler(consolelogfile)
        bb.msg.addDefaultlogFilter(consolelog)
        consolelog.setFormatter(conlogformat)
        logger.addHandler(consolelog)

    llevel, debug_domains = bb.msg.constructLogOptions()
    server.runCommand(["setEventMask", server.getEventHandle(), llevel, debug_domains, _evt_list])

    if not params.observe_only:
        params.updateFromServer(server)
        params.updateToServer(server)
        cmdline = params.parseActions()
        if not cmdline:
            print("Nothing to do.  Use 'bitbake world' to build everything, or run 'bitbake --help' for usage information.")
            return 1
        if 'msg' in cmdline and cmdline['msg']:
            logger.error(cmdline['msg'])
            return 1

        ret, error = server.runCommand(cmdline['action'])
        if error:
            logger.error("Command '%s' failed: %s" % (cmdline, error))
            return 1
        elif ret != True:
            logger.error("Command '%s' failed: returned %s" % (cmdline, ret))
            return 1


    parseprogress = None
    cacheprogress = None
    main.shutdown = 0
    interrupted = False
    return_value = 0
    errors = 0
    warnings = 0
    taskfailures = []

    termfilter = tf(main, helper, console, errconsole, format)
    atexit.register(termfilter.finish)
    stdin_mgr = StdinMgr()

    rtloglevel = RtLogLevel(console, logfilter, mlt, termfilter)
    if bb_rt_loglevel and bb_rt_loglevel != "":
        for inputkey in bb_rt_loglevel:
            rtloglevel.setLevel(inputkey, False)
    while True:
        try:
            event = eventHandler.waitEvent(0)
            if event is None:
                if main.shutdown > 1:
                    break
                termfilter.updateFooter()
                event = eventHandler.waitEvent(0.25)
                if stdin_mgr.poll():
                    keyinput = sys.stdin.read(1)
                    termfilter.clearFooter()
                    if (rtloglevel.setLevel(keyinput, True)):
                        termfilter.updateFooterForce()

                # Always try printing any accumulated log files first
                rtloglevel.displayLogs()
                if event is None:
                    continue

            helper.eventHandler(event)
            if isinstance(event, bb.build.TaskStarted):
                if (rtloglevel.displayLogLocations):
                    termfilter.clearFooter()
                    print "NOTE: LOG: %s" % event.logfile
                mlt.openLog(event.logfile, event.pid)
                rtloglevel.displayLogs()

            if isinstance(event, bb.build.TaskSucceeded):
                mlt.closeLogPid(event.pid)

            if isinstance(event, bb.build.TaskFailed):
                mlt.closeLogPid(event.pid)

            if isinstance(event, bb.runqueue.runQueueExitWait):
                if not main.shutdown:
                    main.shutdown = 1
                continue
            if isinstance(event, bb.event.LogExecTTY):
                if log_exec_tty:
                    tries = event.retries
                    while tries:
                        print("Trying to run: %s" % event.prog)
                        if os.system(event.prog) == 0:
                            break
                        time.sleep(event.sleep_delay)
                        tries -= 1
                    if tries:
                        continue
                logger.warn(event.msg)
                continue

            if isinstance(event, logging.LogRecord):
                if event.levelno >= format.ERROR:
                    errors = errors + 1
                    return_value = 1
                elif event.levelno == format.WARNING:
                    warnings = warnings + 1
                # For "normal" logging conditions, don't show note logs from tasks
                # but do show them if the user has changed the default log level to
                # include verbose/debug messages
                if event.taskpid != 0 and event.levelno <= format.NOTE and (event.levelno < llevel or (event.levelno == format.NOTE and llevel != format.VERBOSE)):
                    continue
                logger.handle(event)
                continue

            if isinstance(event, bb.build.TaskFailedSilent):
                logger.warn("Logfile for failed setscene task is %s" % event.logfile)
                continue
            if isinstance(event, bb.build.TaskFailed):
                return_value = 1
                logfile = event.logfile
                if logfile and os.path.exists(logfile):
                    termfilter.clearFooter()
                    bb.error("Logfile of failure stored in: %s" % logfile)
                    if includelogs and not event.errprinted:
                        print("Log data follows:")
                        f = open(logfile, "r")
                        lines = []
                        while True:
                            l = f.readline()
                            if l == '':
                                break
                            l = l.rstrip()
                            if loglines:
                                lines.append(' | %s' % l)
                                if len(lines) > int(loglines):
                                    lines.pop(0)
                            else:
                                print('| %s' % l)
                        f.close()
                        if lines:
                            for line in lines:
                                print(line)
            if isinstance(event, bb.build.TaskBase):
                logger.info(event._message)
                continue
            if isinstance(event, bb.event.ParseStarted):
                if event.total == 0:
                    continue
                parseprogress = new_progress("Parsing recipes", event.total).start()
                continue
            if isinstance(event, bb.event.ParseProgress):
                parseprogress.update(event.current)
                continue
            if isinstance(event, bb.event.ParseCompleted):
                if not parseprogress:
                    continue

                parseprogress.finish()
                print(("Parsing of %d .bb files complete (%d cached, %d parsed). %d targets, %d skipped, %d masked, %d errors."
                    % ( event.total, event.cached, event.parsed, event.virtuals, event.skipped, event.masked, event.errors)))
                continue

            if isinstance(event, bb.event.CacheLoadStarted):
                cacheprogress = new_progress("Loading cache", event.total).start()
                continue
            if isinstance(event, bb.event.CacheLoadProgress):
                cacheprogress.update(event.current)
                continue
            if isinstance(event, bb.event.CacheLoadCompleted):
                cacheprogress.finish()
                print("Loaded %d entries from dependency cache." % event.num_entries)
                continue

            if isinstance(event, bb.command.CommandFailed):
                return_value = event.exitcode
                if event.error:
                    errors = errors + 1
                    logger.error("Command execution failed: %s", event.error)
                main.shutdown = 2
                continue
            if isinstance(event, bb.command.CommandExit):
                if not return_value:
                    return_value = event.exitcode
                continue
            if isinstance(event, (bb.command.CommandCompleted, bb.cooker.CookerExit)):
                main.shutdown = 2
                continue
            if isinstance(event, bb.event.MultipleProviders):
                logger.info("multiple providers are available for %s%s (%s)", event._is_runtime and "runtime " or "",
                            event._item,
                            ", ".join(event._candidates))
                logger.info("consider defining a PREFERRED_PROVIDER entry to match %s", event._item)
                continue
            if isinstance(event, bb.event.NoProvider):
                return_value = 1
                errors = errors + 1
                if event._runtime:
                    r = "R"
                else:
                    r = ""

                extra = ''
                if not event._reasons:
                    if event._close_matches:
                        extra = ". Close matches:\n  %s" % '\n  '.join(event._close_matches)

                if event._dependees:
                    logger.error("Nothing %sPROVIDES '%s' (but %s %sDEPENDS on or otherwise requires it)%s", r, event._item, ", ".join(event._dependees), r, extra)
                else:
                    logger.error("Nothing %sPROVIDES '%s'%s", r, event._item, extra)
                if event._reasons:
                    for reason in event._reasons:
                        logger.error("%s", reason)
                continue

            if isinstance(event, bb.runqueue.sceneQueueTaskStarted):
                logger.info("Running setscene task %d of %d (%s)" % (event.stats.completed + event.stats.active + event.stats.failed + 1, event.stats.total, event.taskstring))
                continue

            if isinstance(event, bb.runqueue.runQueueTaskStarted):
                if event.noexec:
                    tasktype = 'noexec task'
                else:
                    tasktype = 'task'
                logger.info("Running %s %s of %s (ID: %s, %s)",
                            tasktype,
                            event.stats.completed + event.stats.active +
                                event.stats.failed + 1,
                            event.stats.total, event.taskid, event.taskstring)
                continue

            if isinstance(event, bb.runqueue.runQueueTaskFailed):
                taskfailures.append(event.taskstring)
                logger.error("Task %s (%s) failed with exit code '%s'",
                             event.taskid, event.taskstring, event.exitcode)
                continue

            if isinstance(event, bb.runqueue.sceneQueueTaskFailed):
                logger.warn("Setscene task %s (%s) failed with exit code '%s' - real task will be run instead",
                             event.taskid, event.taskstring, event.exitcode)
                continue

            if isinstance(event, bb.event.DepTreeGenerated):
                continue

            # ignore
            if isinstance(event, (bb.event.BuildBase,
                                  bb.event.MetadataEvent,
                                  bb.event.StampUpdate,
                                  bb.event.ConfigParsed,
                                  bb.event.RecipeParsed,
                                  bb.event.RecipePreFinalise,
                                  bb.runqueue.runQueueEvent,
                                  bb.event.OperationStarted,
                                  bb.event.OperationCompleted,
                                  bb.event.OperationProgress,
                                  bb.event.DiskFull)):
                continue

            logger.error("Unknown event: %s", event)

        except EnvironmentError as ioerror:
            termfilter.clearFooter()
            # ignore interrupted io
            if ioerror.args[0] == 4:
                continue
            sys.stderr.write(str(ioerror))
            if not params.observe_only:
                _, error = server.runCommand(["stateForceShutdown"])
            main.shutdown = 2
        except KeyboardInterrupt:
            stdin_mgr.restore()
            termfilter.clearFooter()
            if params.observe_only:
                print("\nKeyboard Interrupt, exiting observer...")
                main.shutdown = 2
            if not params.observe_only and main.shutdown == 1:
                print("\nSecond Keyboard Interrupt, stopping...\n")
                _, error = server.runCommand(["stateForceShutdown"])
                if error:
                    logger.error("Unable to cleanly stop: %s" % error)
            if not params.observe_only and main.shutdown == 0:
                print("\nKeyboard Interrupt, closing down...\n")
                interrupted = True
                _, error = server.runCommand(["stateShutdown"])
                if error:
                    logger.error("Unable to cleanly shutdown: %s" % error)
            main.shutdown = main.shutdown + 1
            pass
        except Exception as e:
            sys.stderr.write(str(e))
            if not params.observe_only:
                _, error = server.runCommand(["stateForceShutdown"])
            main.shutdown = 2
    stdin_mgr.restore()
    summary = ""
    if taskfailures:
        summary += pluralise("\nSummary: %s task failed:",
                             "\nSummary: %s tasks failed:", len(taskfailures))
        for failure in taskfailures:
            summary += "\n  %s" % failure
    if warnings:
        summary += pluralise("\nSummary: There was %s WARNING message shown.",
                             "\nSummary: There were %s WARNING messages shown.", warnings)
    if return_value and errors:
        summary += pluralise("\nSummary: There was %s ERROR message shown, returning a non-zero exit code.",
                             "\nSummary: There were %s ERROR messages shown, returning a non-zero exit code.", errors)
    if summary:
        print(summary)

    if interrupted:
        print("Execution was interrupted, returning a non-zero exit code.")
        if return_value == 0:
            return_value = 1

    return return_value
