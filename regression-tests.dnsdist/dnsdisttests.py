#!/usr/bin/env python2

import base64
import copy
import errno
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import unittest

import clientsubnetoption

import dns
import dns.message

import libnacl
import libnacl.utils

import h2.connection
import h2.events
import h2.config

import pycurl
from io import BytesIO

from doqclient import quic_query
from doh3client import doh3_query

from eqdnsmessage import AssertEqualDNSMessageMixin
from proxyprotocol import ProxyProtocol

# Python2/3 compatibility hacks
try:
  from queue import Queue
except ImportError:
  from Queue import Queue

try:
  range = xrange
except NameError:
  pass

def getWorkerID():
    if not 'PYTEST_XDIST_WORKER' in os.environ:
      return 0
    workerName = os.environ['PYTEST_XDIST_WORKER']
    return int(workerName[2:])

workerPorts = {}

def pickAvailablePort():
    global workerPorts
    workerID = getWorkerID()
    if workerID in workerPorts:
      port = workerPorts[workerID] + 1
    else:
      port = 11000 + (workerID * 1000)
    workerPorts[workerID] = port
    return port

class ResponderDropAction(object):
    """
    An object to indicate a drop action shall be taken
    """
    pass

class DNSDistTest(AssertEqualDNSMessageMixin, unittest.TestCase):
    """
    Set up a dnsdist instance and responder threads.
    Queries sent to dnsdist are relayed to the responder threads,
    who reply with the response provided by the tests themselves
    on a queue. Responder threads also queue the queries received
    from dnsdist on a separate queue, allowing the tests to check
    that the queries sent from dnsdist were as expected.
    """
    _dnsDistListeningAddr = "127.0.0.1"
    _toResponderQueue = Queue()
    _fromResponderQueue = Queue()
    _queueTimeout = 1
    _dnsdist = None
    _responsesCounter = {}
    _config_template = """
    """
    _config_params = ['_testServerPort']
    _yaml_config_template = None
    _yaml_config_params = []
    _acl = ['127.0.0.1/32']
    _consoleKey = None
    _healthCheckName = 'a.root-servers.net.'
    _healthCheckCounter = 0
    _answerUnexpected = True
    _checkConfigExpectedOutput = None
    _verboseMode = False
    _sudoMode = False
    _skipListeningOnCL = False
    _alternateListeningAddr = None
    _alternateListeningPort = None
    _backgroundThreads = {}
    _UDPResponder = None
    _TCPResponder = None
    _extraStartupSleep = 0
    _dnsDistPort = pickAvailablePort()
    _consolePort = pickAvailablePort()
    _testServerPort = pickAvailablePort()

    @classmethod
    def waitForTCPSocket(cls, ipaddress, port):
        for try_number in range(0, 20):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect((ipaddress, port))
                sock.close()
                return
            except Exception as err:
                if err.errno != errno.ECONNREFUSED:
                    print(f'Error occurred: {try_number} {err}', file=sys.stderr)
            time.sleep(0.1)
       # We assume the dnsdist instance does not listen. That's fine.

    @classmethod
    def startResponders(cls):
        print("Launching responders..")
        cls._testServerPort = pickAvailablePort()

        cls._UDPResponder = threading.Thread(name='UDP Responder', target=cls.UDPResponder, args=[cls._testServerPort, cls._toResponderQueue, cls._fromResponderQueue])
        cls._UDPResponder.daemon = True
        cls._UDPResponder.start()
        cls._TCPResponder = threading.Thread(name='TCP Responder', target=cls.TCPResponder, args=[cls._testServerPort, cls._toResponderQueue, cls._fromResponderQueue])
        cls._TCPResponder.daemon = True
        cls._TCPResponder.start()
        cls.waitForTCPSocket("127.0.0.1", cls._testServerPort);

    @classmethod
    def startDNSDist(cls):
        cls._dnsDistPort = pickAvailablePort()
        cls._consolePort = pickAvailablePort()

        print("Launching dnsdist..")
        if cls._yaml_config_template:
            if 'SKIP_YAML_TESTS' in os.environ:
                raise unittest.SkipTest('YAML tests are disabled')

            params = tuple([getattr(cls, param) for param in cls._yaml_config_params])
            confFile = os.path.join('configs', 'dnsdist_%s.yml' % (cls.__name__))
            with open(confFile, 'w') as conf:
                conf.write(cls._yaml_config_template % params)
                conf.write("\nsecurity_polling:\n  suffix: ''\n")

        params = tuple([getattr(cls, param) for param in cls._config_params])
        print(params)
        extension = 'lua' if cls._yaml_config_template else 'conf'
        luaConfFile = os.path.join('configs', 'dnsdist_%s.%s' % (cls.__name__, extension))
        if not cls._yaml_config_template:
          confFile = luaConfFile

        if len(cls._config_template.strip(' \n\t')) > 0:
          with open(luaConfFile, 'w') as conf:
            conf.write("-- Autogenerated by dnsdisttests.py\n")
            conf.write(f"-- dnsdist will listen on {cls._dnsDistPort}\n")
            conf.write(cls._config_template % params)
            if not cls._yaml_config_template:
              conf.write("\n")
              conf.write("setSecurityPollSuffix('')")
        else:
          try:
            os.unlink(luaConfFile)
          except OSError:
            pass

        if cls._skipListeningOnCL:
          dnsdistcmd = [os.environ['DNSDISTBIN'], '--supervised', '-C', confFile ]
        else:
          dnsdistcmd = [os.environ['DNSDISTBIN'], '--supervised', '-C', confFile,
                        '-l', '%s:%d' % (cls._dnsDistListeningAddr, cls._dnsDistPort) ]

        if cls._verboseMode:
            dnsdistcmd.append('-v')
        if cls._sudoMode:
            preserve_env_values = ['LD_LIBRARY_PATH', 'LLVM_PROFILE_FILE']
            for value in preserve_env_values:
                if value in os.environ:
                    dnsdistcmd.insert(0, value + '=' + os.environ[value])
            dnsdistcmd.insert(0, 'sudo')

        for acl in cls._acl:
            dnsdistcmd.extend(['--acl', acl])
        print(' '.join(dnsdistcmd))

        # validate config with --check-config, which sets client=true, possibly exposing bugs.
        testcmd = dnsdistcmd + ['--check-config']
        try:
            output = subprocess.check_output(testcmd, stderr=subprocess.STDOUT, close_fds=True)
        except subprocess.CalledProcessError as exc:
            raise AssertionError('dnsdist --check-config failed (%d): %s' % (exc.returncode, exc.output))
        if cls._checkConfigExpectedOutput is not None:
          expectedOutput = cls._checkConfigExpectedOutput
        else:
          expectedOutput = ('Configuration \'%s\' OK!\n' % (confFile)).encode()
        if not cls._verboseMode and output != expectedOutput:
            raise AssertionError('dnsdist --check-config failed: %s (expected %s)' % (output, expectedOutput))

        logFile = os.path.join('configs', 'dnsdist_%s.log' % (cls.__name__))
        with open(logFile, 'w') as fdLog:
          cls._dnsdist = subprocess.Popen(dnsdistcmd, close_fds=True, stdout=fdLog, stderr=fdLog)

        if cls._alternateListeningAddr and cls._alternateListeningPort:
            cls.waitForTCPSocket(cls._alternateListeningAddr, cls._alternateListeningPort)
        else:
            cls.waitForTCPSocket(cls._dnsDistListeningAddr, cls._dnsDistPort)

        if cls._dnsdist.poll() is not None:
            print(f"\n*** startDNSDist log for {logFile} ***")
            with open(logFile, 'r') as fdLog:
                print(fdLog.read())
            print(f"*** End startDNSDist log for {logFile} ***")
            raise AssertionError('%s failed (%d)' % (dnsdistcmd, cls._dnsdist.returncode))
        time.sleep(cls._extraStartupSleep)

    @classmethod
    def setUpSockets(cls):
        print("Setting up UDP socket..")
        cls._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls._sock.settimeout(2.0)
        cls._sock.connect(("127.0.0.1", cls._dnsDistPort))

    @classmethod
    def killProcess(cls, p):
        # Don't try to kill it if it's already dead
        if p.poll() is not None:
            return
        try:
            p.terminate()
            for count in range(50):
                x = p.poll()
                if x is not None:
                    break
                time.sleep(0.1)
            if x is None:
                print("kill...", p, file=sys.stderr)
                p.kill()
                p.wait()
            if p.returncode != 0:
              if p.returncode < 0:
                raise AssertionError('Process was killed by signal %d' % (-p.returncode))
              else:
                raise AssertionError('Process exited with return code %d' % (p.returncode))
        except OSError as e:
            # There is a race-condition with the poll() and
            # kill() statements, when the process is dead on the
            # kill(), this is fine
            if e.errno != errno.ESRCH:
                raise

    @classmethod
    def setUpClass(cls):

        cls.startResponders()
        cls.startDNSDist()
        cls.setUpSockets()

        print("Launching tests..")

    @classmethod
    def tearDownClass(cls):
        cls._sock.close()
        # tell the background threads to stop, if any
        for backgroundThread in cls._backgroundThreads:
            cls._backgroundThreads[backgroundThread] = False
        cls.killProcess(cls._dnsdist)

    @classmethod
    def _ResponderIncrementCounter(cls):
        if threading.current_thread().name in cls._responsesCounter:
            cls._responsesCounter[threading.current_thread().name] += 1
        else:
            cls._responsesCounter[threading.current_thread().name] = 1

    @classmethod
    def _getResponse(cls, request, fromQueue, toQueue, synthesize=None):
        response = None
        if len(request.question) != 1:
            print("Skipping query with question count %d" % (len(request.question)))
            return None
        healthCheck = str(request.question[0].name).endswith(cls._healthCheckName)
        if healthCheck:
            cls._healthCheckCounter += 1
            response = dns.message.make_response(request)
        else:
            cls._ResponderIncrementCounter()
            if not fromQueue.empty():
                toQueue.put(request, True, cls._queueTimeout)
                response = fromQueue.get(True, cls._queueTimeout)
                if response:
                  response = copy.copy(response)
                  response.id = request.id

        if synthesize is not None:
          response = dns.message.make_response(request)
          response.set_rcode(synthesize)

        if not response:
            if cls._answerUnexpected:
                response = dns.message.make_response(request)
                response.set_rcode(dns.rcode.SERVFAIL)

        return response

    @classmethod
    def UDPResponder(cls, port, fromQueue, toQueue, trailingDataResponse=False, callback=None):
        cls._backgroundThreads[threading.get_native_id()] = True
        # trailingDataResponse=True means "ignore trailing data".
        # Other values are either False (meaning "raise an exception")
        # or are interpreted as a response RCODE for queries with trailing data.
        # callback is invoked for every -even healthcheck ones- query and should return a raw response
        ignoreTrailing = trailingDataResponse is True

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("127.0.0.1", port))
        sock.settimeout(0.5)
        while True:
            try:
              data, addr = sock.recvfrom(4096)
            except socket.timeout:
              if cls._backgroundThreads.get(threading.get_native_id(), False) == False:
                del cls._backgroundThreads[threading.get_native_id()]
                break
              else:
                continue

            forceRcode = None
            try:
                request = dns.message.from_wire(data, ignore_trailing=ignoreTrailing)
            except dns.message.TrailingJunk as e:
                print('trailing data exception in UDPResponder')
                if trailingDataResponse is False or forceRcode is True:
                    raise
                print("UDP query with trailing data, synthesizing response")
                request = dns.message.from_wire(data, ignore_trailing=True)
                forceRcode = trailingDataResponse

            wire = None
            if callback:
              wire = callback(request)
            else:
              if request.edns > 1:
                forceRcode = dns.rcode.BADVERS
              response = cls._getResponse(request, fromQueue, toQueue, synthesize=forceRcode)
              if response:
                wire = response.to_wire()

            if not wire:
              continue
            elif isinstance(wire, ResponderDropAction):
              continue

            sock.sendto(wire, addr)

        sock.close()

    @classmethod
    def handleTCPConnection(cls, conn, fromQueue, toQueue, trailingDataResponse=False, multipleResponses=False, callback=None, partialWrite=False):
      ignoreTrailing = trailingDataResponse is True
      try:
        data = conn.recv(2)
      except Exception as err:
        data = None
        print(f'Error while reading query size in TCP responder thread {err=}, {type(err)=}')
      if not data:
        conn.close()
        return

      (datalen,) = struct.unpack("!H", data)
      data = conn.recv(datalen)
      forceRcode = None
      try:
        request = dns.message.from_wire(data, ignore_trailing=ignoreTrailing)
      except dns.message.TrailingJunk as e:
        if trailingDataResponse is False or forceRcode is True:
          raise
        print("TCP query with trailing data, synthesizing response")
        request = dns.message.from_wire(data, ignore_trailing=True)
        forceRcode = trailingDataResponse

      if callback:
        wire = callback(request)
      else:
        if request.edns > 1:
          forceRcode = dns.rcode.BADVERS
        response = cls._getResponse(request, fromQueue, toQueue, synthesize=forceRcode)
        if response:
          wire = response.to_wire(max_size=65535)

      if not wire:
        conn.close()
        return
      elif isinstance(wire, ResponderDropAction):
        return

      wireLen = struct.pack("!H", len(wire))
      if partialWrite:
        for b in wireLen:
          conn.send(bytes([b]))
          time.sleep(0.5)
      else:
        conn.send(wireLen)
      conn.send(wire)

      while multipleResponses:
        # do not block, and stop as soon as the queue is empty, either the next response is already here or we are done
        # otherwise we might read responses intended for the next connection
        if fromQueue.empty():
          break

        response = fromQueue.get(False)
        if not response:
          break

        response = copy.copy(response)
        response.id = request.id
        wire = response.to_wire(max_size=65535)
        try:
          conn.send(struct.pack("!H", len(wire)))
          conn.send(wire)
        except socket.error as e:
          # some of the tests are going to close
          # the connection on us, just deal with it
          break

      conn.close()

    @classmethod
    def TCPResponder(cls, port, fromQueue, toQueue, trailingDataResponse=False, multipleResponses=False, callback=None, tlsContext=None, multipleConnections=False, listeningAddr='127.0.0.1', partialWrite=False):
        cls._backgroundThreads[threading.get_native_id()] = True
        # trailingDataResponse=True means "ignore trailing data".
        # Other values are either False (meaning "raise an exception")
        # or are interpreted as a response RCODE for queries with trailing data.
        # callback is invoked for every -even healthcheck ones- query and should return a raw response

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind((listeningAddr, port))
        except socket.error as e:
            print("Error binding in the TCP responder: %s" % str(e))
            sys.exit(1)

        sock.listen(100)
        sock.settimeout(0.5)
        if tlsContext:
          sock = tlsContext.wrap_socket(sock, server_side=True)

        while True:
            try:
              (conn, _) = sock.accept()
            except ssl.SSLError:
              continue
            except ConnectionResetError:
              continue
            except socket.timeout:
              if cls._backgroundThreads.get(threading.get_native_id(), False) == False:
                 del cls._backgroundThreads[threading.get_native_id()]
                 break
              else:
                continue

            conn.settimeout(5.0)
            if multipleConnections:
              thread = threading.Thread(name='TCP Connection Handler',
                                        target=cls.handleTCPConnection,
                                        args=[conn, fromQueue, toQueue, trailingDataResponse, multipleResponses, callback, partialWrite])
              thread.daemon = True
              thread.start()
            else:
              cls.handleTCPConnection(conn, fromQueue, toQueue, trailingDataResponse, multipleResponses, callback, partialWrite)

        sock.close()

    @classmethod
    def handleDoHConnection(cls, config, conn, fromQueue, toQueue, trailingDataResponse, multipleResponses, callback, tlsContext, useProxyProtocol):
        ignoreTrailing = trailingDataResponse is True
        try:
          h2conn = h2.connection.H2Connection(config=config)
          h2conn.initiate_connection()
          conn.sendall(h2conn.data_to_send())
        except ssl.SSLEOFError as e:
          print("Unexpected EOF: %s" % (e))
          return
        except Exception as err:
          print(f'Unexpected exception in DoH responder thread (connection init) {err=}, {type(err)=}')
          return

        dnsData = {}

        if useProxyProtocol:
            # try to read the entire Proxy Protocol header
            proxy = ProxyProtocol()
            header = conn.recv(proxy.HEADER_SIZE)
            if not header:
                print('unable to get header')
                conn.close()
                return

            if not proxy.parseHeader(header):
                print('unable to parse header')
                print(header)
                conn.close()
                return

            proxyContent = conn.recv(proxy.contentLen)
            if not proxyContent:
                print('unable to get content')
                conn.close()
                return

            payload = header + proxyContent
            toQueue.put(payload, True, cls._queueTimeout)

        # be careful, HTTP/2 headers and data might be in different recv() results
        requestHeaders = None
        while True:
            try:
              data = conn.recv(65535)
            except Exception as err:
              data = None
              print(f'Unexpected exception in DoH responder thread {err=}, {type(err)=}')
            if not data:
                break

            events = h2conn.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.RequestReceived):
                    requestHeaders = event.headers
                if isinstance(event, h2.events.DataReceived):
                    h2conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    if not event.stream_id in dnsData:
                      dnsData[event.stream_id] = b''
                    dnsData[event.stream_id] = dnsData[event.stream_id] + (event.data)
                    if event.stream_ended:
                        forceRcode = None
                        status = 200
                        try:
                            request = dns.message.from_wire(dnsData[event.stream_id], ignore_trailing=ignoreTrailing)
                        except dns.message.TrailingJunk as e:
                            if trailingDataResponse is False or forceRcode is True:
                                raise
                            print("DOH query with trailing data, synthesizing response")
                            request = dns.message.from_wire(dnsData[event.stream_id], ignore_trailing=True)
                            forceRcode = trailingDataResponse

                        if callback:
                            status, wire = callback(request, requestHeaders, fromQueue, toQueue)
                        else:
                            response = cls._getResponse(request, fromQueue, toQueue, synthesize=forceRcode)
                            if response:
                                wire = response.to_wire(max_size=65535)

                        if not wire:
                            conn.close()
                            conn = None
                            break
                        elif isinstance(wire, ResponderDropAction):
                            break

                        headers = [
                          (':status', str(status)),
                          ('content-length', str(len(wire))),
                          ('content-type', 'application/dns-message'),
                        ]
                        h2conn.send_headers(stream_id=event.stream_id, headers=headers)
                        h2conn.send_data(stream_id=event.stream_id, data=wire, end_stream=True)

                data_to_send = h2conn.data_to_send()
                if data_to_send:
                    conn.sendall(data_to_send)

            if conn is None:
                break

        if conn is not None:
            conn.close()

    @classmethod
    def DOHResponder(cls, port, fromQueue, toQueue, trailingDataResponse=False, multipleResponses=False, callback=None, tlsContext=None, useProxyProtocol=False):
        cls._backgroundThreads[threading.get_native_id()] = True
        # trailingDataResponse=True means "ignore trailing data".
        # Other values are either False (meaning "raise an exception")
        # or are interpreted as a response RCODE for queries with trailing data.
        # callback is invoked for every -even healthcheck ones- query and should return a raw response

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except socket.error as e:
            print("Error binding in the TCP responder: %s" % str(e))
            sys.exit(1)

        sock.listen(100)
        sock.settimeout(0.5)
        if tlsContext:
            sock = tlsContext.wrap_socket(sock, server_side=True)

        config = h2.config.H2Configuration(client_side=False)

        while True:
            try:
                (conn, _) = sock.accept()
            except ssl.SSLError:
                continue
            except ConnectionResetError:
              continue
            except socket.timeout:
                if cls._backgroundThreads.get(threading.get_native_id(), False) == False:
                    del cls._backgroundThreads[threading.get_native_id()]
                    break
                else:
                    continue

            conn.settimeout(5.0)
            thread = threading.Thread(name='DoH Connection Handler',
                                      target=cls.handleDoHConnection,
                                      args=[config, conn, fromQueue, toQueue, trailingDataResponse, multipleResponses, callback, tlsContext, useProxyProtocol])
            thread.daemon = True
            thread.start()

        sock.close()

    @classmethod
    def sendUDPQuery(cls, query, response, useQueue=True, timeout=2.0, rawQuery=False):
        if useQueue and response is not None:
            cls._toResponderQueue.put(response, True, timeout)

        if timeout:
            cls._sock.settimeout(timeout)

        try:
            if not rawQuery:
                query = query.to_wire()
            cls._sock.send(query)
            data = cls._sock.recv(4096)
        except socket.timeout:
            data = None
        finally:
            if timeout:
                cls._sock.settimeout(None)

        receivedQuery = None
        message = None
        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
        if data:
            message = dns.message.from_wire(data)
        return (receivedQuery, message)

    @classmethod
    def openTCPConnection(cls, timeout=2.0, port=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if timeout:
            sock.settimeout(timeout)

        if not port:
          port = cls._dnsDistPort

        sock.connect(("127.0.0.1", port))
        return sock

    @classmethod
    def openTLSConnection(cls, port, serverName, caCert=None, timeout=2.0, alpn=[], sslctx=None, session=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if timeout:
            sock.settimeout(timeout)

        # 2.7.9+
        if hasattr(ssl, 'create_default_context'):
            if not sslctx:
                sslctx = ssl.create_default_context(cafile=caCert)
                if len(alpn)> 0 and hasattr(sslctx, 'set_alpn_protocols'):
                    sslctx.set_alpn_protocols(alpn)
            sslsock = sslctx.wrap_socket(sock, server_hostname=serverName, session=session)
        else:
            sslsock = ssl.wrap_socket(sock, ca_certs=caCert, cert_reqs=ssl.CERT_REQUIRED)

        sslsock.connect(("127.0.0.1", port))
        return sslsock

    @classmethod
    def sendTCPQueryOverConnection(cls, sock, query, rawQuery=False, response=None, timeout=2.0):
        if not rawQuery:
            wire = query.to_wire()
        else:
            wire = query

        if response:
            cls._toResponderQueue.put(response, True, timeout)

        sock.send(struct.pack("!H", len(wire)))
        sock.send(wire)

    @classmethod
    def recvTCPResponseOverConnection(cls, sock, useQueue=False, timeout=2.0):
        message = None
        data = sock.recv(2)
        if data:
            (datalen,) = struct.unpack("!H", data)
            print(datalen)
            data = sock.recv(datalen)
            if data:
                print(data)
                message = dns.message.from_wire(data)

        print(useQueue)
        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
            print(receivedQuery)
            return (receivedQuery, message)
        else:
            print("queue empty")
            return message

    @classmethod
    def sendDOTQuery(cls, port, serverName, query, response, caFile, useQueue=True, timeout=None):
        conn = cls.openTLSConnection(port, serverName, caFile, timeout=timeout)
        cls.sendTCPQueryOverConnection(conn, query, response=response, timeout=timeout)
        if useQueue:
          return cls.recvTCPResponseOverConnection(conn, useQueue=useQueue, timeout=timeout)
        return None, cls.recvTCPResponseOverConnection(conn, useQueue=useQueue, timeout=timeout)

    @classmethod
    def sendTCPQuery(cls, query, response, useQueue=True, timeout=2.0, rawQuery=False):
        message = None
        if useQueue:
            cls._toResponderQueue.put(response, True, timeout)

        try:
            sock = cls.openTCPConnection(timeout)
        except socket.timeout as e:
            print("Timeout while opening TCP connection: %s" % (str(e)))
            return (None, None)

        try:
            cls.sendTCPQueryOverConnection(sock, query, rawQuery, timeout=timeout)
            message = cls.recvTCPResponseOverConnection(sock, timeout=timeout)
        except socket.timeout as e:
            print("Timeout while sending or receiving TCP data: %s" % (str(e)))
        except socket.error as e:
            print("Network error: %s" % (str(e)))
        finally:
            sock.close()

        receivedQuery = None
        print(useQueue)
        if useQueue and not cls._fromResponderQueue.empty():
            print(receivedQuery)
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
        else:
          print("queue is empty")

        return (receivedQuery, message)

    @classmethod
    def sendTCPQueryWithMultipleResponses(cls, query, responses, useQueue=True, timeout=2.0, rawQuery=False):
        if useQueue:
            for response in responses:
                cls._toResponderQueue.put(response, True, timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if timeout:
            sock.settimeout(timeout)

        sock.connect(("127.0.0.1", cls._dnsDistPort))
        messages = []

        try:
            if not rawQuery:
                wire = query.to_wire()
            else:
                wire = query

            sock.send(struct.pack("!H", len(wire)))
            sock.send(wire)
            while True:
                data = sock.recv(2)
                if not data:
                    break
                (datalen,) = struct.unpack("!H", data)
                data = sock.recv(datalen)
                messages.append(dns.message.from_wire(data))

        except socket.timeout as e:
            print("Timeout while receiving multiple TCP responses: %s" % (str(e)))
        except socket.error as e:
            print("Network error: %s" % (str(e)))
        finally:
            sock.close()

        receivedQuery = None
        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
        return (receivedQuery, messages)

    def setUp(self):
        # This function is called before every test

        # Clear the responses counters
        self._responsesCounter.clear()

        self._healthCheckCounter = 0

        # Make sure the queues are empty, in case
        # a previous test failed
        self.clearResponderQueues()

        super(DNSDistTest, self).setUp()

    @classmethod
    def clearToResponderQueue(cls):
        while not cls._toResponderQueue.empty():
            cls._toResponderQueue.get(False)

    @classmethod
    def clearFromResponderQueue(cls):
        while not cls._fromResponderQueue.empty():
            cls._fromResponderQueue.get(False)

    @classmethod
    def clearResponderQueues(cls):
        cls.clearToResponderQueue()
        cls.clearFromResponderQueue()

    @staticmethod
    def generateConsoleKey():
        return libnacl.utils.salsa_key()

    @classmethod
    def _encryptConsole(cls, command, nonce):
        command = command.encode('UTF-8')
        if cls._consoleKey is None:
            return command
        return libnacl.crypto_secretbox(command, nonce, cls._consoleKey)

    @classmethod
    def _decryptConsole(cls, command, nonce):
        if cls._consoleKey is None:
            result = command
        else:
            result = libnacl.crypto_secretbox_open(command, nonce, cls._consoleKey)
        return result.decode('UTF-8')

    @classmethod
    def sendConsoleCommand(cls, command, timeout=5.0, IPv6=False):
        ourNonce = libnacl.utils.rand_nonce()
        theirNonce = None
        sock = socket.socket(socket.AF_INET if not IPv6 else socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if timeout:
            sock.settimeout(timeout)

        sock.connect(("::1", cls._consolePort, 0, 0) if IPv6 else ("127.0.0.1", cls._consolePort))
        sock.send(ourNonce)
        theirNonce = sock.recv(len(ourNonce))
        if len(theirNonce) != len(ourNonce):
            print("Received a nonce of size %d, expecting %d, console command will not be sent!" % (len(theirNonce), len(ourNonce)))
            if len(theirNonce) == 0:
                raise socket.error("Got EOF while reading a nonce of size %d, console command will not be sent!" % (len(ourNonce)))
            return None

        halfNonceSize = int(len(ourNonce) / 2)
        readingNonce = ourNonce[0:halfNonceSize] + theirNonce[halfNonceSize:]
        writingNonce = theirNonce[0:halfNonceSize] + ourNonce[halfNonceSize:]
        msg = cls._encryptConsole(command, writingNonce)
        sock.send(struct.pack("!I", len(msg)))
        sock.send(msg)
        data = sock.recv(4)
        if not data:
            raise socket.error("Got EOF while reading the response size")

        (responseLen,) = struct.unpack("!I", data)
        data = sock.recv(responseLen)
        response = cls._decryptConsole(data, readingNonce)
        sock.close()
        return response

    def compareOptions(self, a, b):
        self.assertEqual(len(a), len(b))
        for idx in range(len(a)):
            self.assertEqual(a[idx], b[idx])

    def checkMessageNoEDNS(self, expected, received):
        self.assertEqual(expected, received)
        self.assertEqual(received.edns, -1)
        self.assertEqual(len(received.options), 0)

    def checkMessageEDNSWithoutOptions(self, expected, received):
        self.assertEqual(expected, received)
        self.assertEqual(received.edns, 0)
        self.assertEqual(expected.ednsflags, received.ednsflags)
        self.assertEqual(expected.payload, received.payload)

    def checkMessageEDNSWithoutECS(self, expected, received, withCookies=0):
        self.assertEqual(expected, received)
        self.assertEqual(received.edns, 0)
        self.assertEqual(expected.ednsflags, received.ednsflags)
        self.assertEqual(expected.payload, received.payload)
        self.assertEqual(len(received.options), withCookies)
        if withCookies:
            for option in received.options:
                self.assertEqual(option.otype, 10)
        else:
            for option in received.options:
                self.assertNotEqual(option.otype, 10)

    def checkMessageEDNSWithECS(self, expected, received, additionalOptions=0):
        self.assertEqual(expected, received)
        self.assertEqual(received.edns, 0)
        self.assertEqual(expected.ednsflags, received.ednsflags)
        self.assertEqual(expected.payload, received.payload)
        self.assertEqual(len(received.options), 1 + additionalOptions)
        hasECS = False
        for option in received.options:
            if option.otype == clientsubnetoption.ASSIGNED_OPTION_CODE:
                hasECS = True
            else:
                self.assertNotEqual(additionalOptions, 0)

        self.compareOptions(expected.options, received.options)
        self.assertTrue(hasECS)

    def checkMessageEDNS(self, expected, received):
        self.assertEqual(expected, received)
        self.assertEqual(received.edns, 0)
        self.assertEqual(expected.ednsflags, received.ednsflags)
        self.assertEqual(expected.payload, received.payload)
        self.assertEqual(len(expected.options), len(received.options))
        self.compareOptions(expected.options, received.options)

    def checkQueryEDNSWithECS(self, expected, received, additionalOptions=0):
        self.checkMessageEDNSWithECS(expected, received, additionalOptions)

    def checkQueryEDNS(self, expected, received):
        self.checkMessageEDNS(expected, received)

    def checkResponseEDNSWithECS(self, expected, received, additionalOptions=0):
        self.checkMessageEDNSWithECS(expected, received, additionalOptions)

    def checkQueryEDNSWithoutECS(self, expected, received):
        self.checkMessageEDNSWithoutECS(expected, received)

    def checkResponseEDNSWithoutECS(self, expected, received, withCookies=0):
        self.checkMessageEDNSWithoutECS(expected, received, withCookies)

    def checkQueryNoEDNS(self, expected, received):
        self.checkMessageNoEDNS(expected, received)

    def checkResponseNoEDNS(self, expected, received):
        self.checkMessageNoEDNS(expected, received)

    @staticmethod
    def generateNewCertificateAndKey(filePrefix):
        # generate and sign a new cert
        cmd = ['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', filePrefix + '.key', '-out', filePrefix + '.csr', '-config', 'configServer.conf']
        output = None
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
            output = process.communicate(input='')
        except subprocess.CalledProcessError as exc:
            raise AssertionError('openssl req failed (%d): %s' % (exc.returncode, exc.output))
        cmd = ['openssl', 'x509', '-req', '-days', '1', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-in', filePrefix + '.csr', '-out', filePrefix + '.pem', '-extfile', 'configServer.conf', '-extensions', 'v3_req']
        output = None
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
            output = process.communicate(input='')
        except subprocess.CalledProcessError as exc:
            raise AssertionError('openssl x509 failed (%d): %s' % (exc.returncode, exc.output))

        with open(filePrefix + '.chain', 'w') as outFile:
            for inFileName in [filePrefix + '.pem', 'ca.pem']:
                with open(inFileName) as inFile:
                    outFile.write(inFile.read())

        cmd = ['openssl', 'pkcs12', '-export', '-passout', 'pass:passw0rd', '-clcerts', '-in', filePrefix + '.pem', '-CAfile', 'ca.pem', '-inkey', filePrefix + '.key', '-out', filePrefix + '.p12']
        output = None
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
            output = process.communicate(input='')
        except subprocess.CalledProcessError as exc:
            raise AssertionError('openssl pkcs12 failed (%d): %s' % (exc.returncode, exc.output))

    def checkMessageProxyProtocol(self, receivedProxyPayload, source, destination, isTCP, values=[], v6=False, sourcePort=None, destinationPort=None):
        proxy = ProxyProtocol()
        self.assertTrue(proxy.parseHeader(receivedProxyPayload))
        self.assertEqual(proxy.version, 0x02)
        self.assertEqual(proxy.command, 0x01)
        if v6:
            self.assertEqual(proxy.family, 0x02)
        else:
            self.assertEqual(proxy.family, 0x01)
        if not isTCP:
            self.assertEqual(proxy.protocol, 0x02)
        else:
            self.assertEqual(proxy.protocol, 0x01)
        self.assertGreater(proxy.contentLen, 0)

        self.assertTrue(proxy.parseAddressesAndPorts(receivedProxyPayload))
        self.assertEqual(proxy.source, source)
        self.assertEqual(proxy.destination, destination)
        if sourcePort:
            self.assertEqual(proxy.sourcePort, sourcePort)
        if destinationPort:
            self.assertEqual(proxy.destinationPort, destinationPort)
        else:
            self.assertEqual(proxy.destinationPort, self._dnsDistPort)

        self.assertTrue(proxy.parseAdditionalValues(receivedProxyPayload))
        proxy.values.sort()
        values.sort()
        self.assertEqual(proxy.values, values)

    @classmethod
    def getDOHGetURL(cls, baseurl, query, rawQuery=False):
        if rawQuery:
            wire = query
        else:
            wire = query.to_wire()
        param = base64.urlsafe_b64encode(wire).decode('UTF8').rstrip('=')
        return baseurl + "?dns=" + param

    @classmethod
    def openDOHConnection(cls, port, caFile, timeout=2.0):
        conn = pycurl.Curl()
        conn.setopt(pycurl.HTTP_VERSION, pycurl.CURL_HTTP_VERSION_2)

        conn.setopt(pycurl.HTTPHEADER, ["Content-type: application/dns-message",
                                         "Accept: application/dns-message"])
        if timeout:
          conn.setopt(pycurl.TIMEOUT_MS, int(timeout*1000))

        return conn

    @classmethod
    def sendDOHQuery(cls, port, servername, baseurl, query, response=None, timeout=2.0, caFile=None, useQueue=True, rawQuery=False, rawResponse=False, customHeaders=[], useHTTPS=True, fromQueue=None, toQueue=None, conn=None):
        url = cls.getDOHGetURL(baseurl, query, rawQuery)

        if not conn:
            conn = cls.openDOHConnection(port, caFile=caFile, timeout=timeout)
            # this means "really do HTTP/2, not HTTP/1 with Upgrade headers"
            conn.setopt(pycurl.HTTP_VERSION, pycurl.CURL_HTTP_VERSION_2_PRIOR_KNOWLEDGE)

        if useHTTPS:
            conn.setopt(pycurl.SSL_VERIFYPEER, 1)
            conn.setopt(pycurl.SSL_VERIFYHOST, 2)
            if caFile:
                conn.setopt(pycurl.CAINFO, caFile)

        response_headers = BytesIO()
        #conn.setopt(pycurl.VERBOSE, True)
        conn.setopt(pycurl.URL, url)
        conn.setopt(pycurl.RESOLVE, ["%s:%d:127.0.0.1" % (servername, port)])

        conn.setopt(pycurl.HTTPHEADER, customHeaders)
        conn.setopt(pycurl.HEADERFUNCTION, response_headers.write)

        if response:
            if toQueue:
                toQueue.put(response, True, timeout)
            else:
                cls._toResponderQueue.put(response, True, timeout)

        receivedQuery = None
        message = None
        cls._response_headers = ''
        data = conn.perform_rb()
        cls._rcode = conn.getinfo(pycurl.RESPONSE_CODE)
        if cls._rcode == 200 and not rawResponse:
            message = dns.message.from_wire(data)
        elif rawResponse:
            message = data

        if useQueue:
            if fromQueue:
                if not fromQueue.empty():
                    receivedQuery = fromQueue.get(True, timeout)
            else:
                if not cls._fromResponderQueue.empty():
                    receivedQuery = cls._fromResponderQueue.get(True, timeout)

        cls._response_headers = response_headers.getvalue()
        return (receivedQuery, message)

    @classmethod
    def sendDOHPostQuery(cls, port, servername, baseurl, query, response=None, timeout=2.0, caFile=None, useQueue=True, rawQuery=False, rawResponse=False, customHeaders=[], useHTTPS=True):
        url = baseurl
        conn = cls.openDOHConnection(port, caFile=caFile, timeout=timeout)
        response_headers = BytesIO()
        #conn.setopt(pycurl.VERBOSE, True)
        conn.setopt(pycurl.URL, url)
        conn.setopt(pycurl.RESOLVE, ["%s:%d:127.0.0.1" % (servername, port)])
        # this means "really do HTTP/2, not HTTP/1 with Upgrade headers"
        conn.setopt(pycurl.HTTP_VERSION, pycurl.CURL_HTTP_VERSION_2_PRIOR_KNOWLEDGE)
        if useHTTPS:
            conn.setopt(pycurl.SSL_VERIFYPEER, 1)
            conn.setopt(pycurl.SSL_VERIFYHOST, 2)
            if caFile:
                conn.setopt(pycurl.CAINFO, caFile)

        conn.setopt(pycurl.HTTPHEADER, customHeaders)
        conn.setopt(pycurl.HEADERFUNCTION, response_headers.write)
        conn.setopt(pycurl.POST, True)
        data = query
        if not rawQuery:
            data = data.to_wire()

        conn.setopt(pycurl.POSTFIELDS, data)

        if response:
            cls._toResponderQueue.put(response, True, timeout)

        receivedQuery = None
        message = None
        cls._response_headers = ''
        data = conn.perform_rb()
        cls._rcode = conn.getinfo(pycurl.RESPONSE_CODE)
        if cls._rcode == 200 and not rawResponse:
            message = dns.message.from_wire(data)
        elif rawResponse:
            message = data

        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)

        cls._response_headers = response_headers.getvalue()
        return (receivedQuery, message)

    def sendDOHQueryWrapper(self, query, response, useQueue=True, timeout=2):
        return self.sendDOHQuery(self._dohServerPort, self._serverName, self._dohBaseURL, query, response=response, caFile=self._caCert, useQueue=useQueue, timeout=timeout)

    def sendDOHWithNGHTTP2QueryWrapper(self, query, response, useQueue=True, timeout=2):
        return self.sendDOHQuery(self._dohWithNGHTTP2ServerPort, self._serverName, self._dohWithNGHTTP2BaseURL, query, response=response, caFile=self._caCert, useQueue=useQueue, timeout=timeout)

    def sendDOHWithH2OQueryWrapper(self, query, response, useQueue=True, timeout=2):
        return self.sendDOHQuery(self._dohWithH2OServerPort, self._serverName, self._dohWithH2OBaseURL, query, response=response, caFile=self._caCert, useQueue=useQueue, timeout=timeout)

    def sendDOTQueryWrapper(self, query, response, useQueue=True, timeout=2):
        return self.sendDOTQuery(self._tlsServerPort, self._serverName, query, response, self._caCert, useQueue=useQueue, timeout=timeout)

    def sendDOQQueryWrapper(self, query, response, useQueue=True, timeout=2):
        return self.sendDOQQuery(self._doqServerPort, query, response=response, caFile=self._caCert, useQueue=useQueue, serverName=self._serverName, timeout=timeout)

    def sendDOH3QueryWrapper(self, query, response, useQueue=True, timeout=2):
        return self.sendDOH3Query(self._doh3ServerPort, self._dohBaseURL, query, response=response, caFile=self._caCert, useQueue=useQueue, serverName=self._serverName, timeout=timeout)
    @classmethod
    def getDOQConnection(cls, port, caFile=None, source=None, source_port=0):

        manager = dns.quic.SyncQuicManager(
            verify_mode=caFile
        )

        return manager.connect('127.0.0.1', port, source, source_port)

    @classmethod
    def sendDOQQuery(cls, port, query, response=None, timeout=2.0, caFile=None, useQueue=True, rawQuery=False, fromQueue=None, toQueue=None, connection=None, serverName=None):

        if response:
            if toQueue:
                toQueue.put(response, True, timeout)
            else:
                cls._toResponderQueue.put(response, True, timeout)

        (message, _) = quic_query(query, '127.0.0.1', timeout, port, verify=caFile, server_hostname=serverName)

        receivedQuery = None

        if useQueue:
            if fromQueue:
                if not fromQueue.empty():
                    receivedQuery = fromQueue.get(True, timeout)
            else:
                if not cls._fromResponderQueue.empty():
                    receivedQuery = cls._fromResponderQueue.get(True, timeout)

        return (receivedQuery, message)

    @classmethod
    def sendDOH3Query(cls, port, baseurl, query, response=None, timeout=2.0, caFile=None, useQueue=True, rawQuery=False, fromQueue=None, toQueue=None, connection=None, serverName=None, post=False, customHeaders=None, rawResponse=False):

        if response:
            if toQueue:
                toQueue.put(response, True, timeout)
            else:
                cls._toResponderQueue.put(response, True, timeout)

        if rawResponse:
          return doh3_query(query, baseurl, timeout, port, verify=caFile, server_hostname=serverName, post=post, additional_headers=customHeaders, raw_response=rawResponse)

        message = doh3_query(query, baseurl, timeout, port, verify=caFile, server_hostname=serverName, post=post, additional_headers=customHeaders, raw_response=rawResponse)

        receivedQuery = None

        if useQueue:
            if fromQueue:
                if not fromQueue.empty():
                    receivedQuery = fromQueue.get(True, timeout)
            else:
                if not cls._fromResponderQueue.empty():
                    receivedQuery = cls._fromResponderQueue.get(True, timeout)

        return (receivedQuery, message)
