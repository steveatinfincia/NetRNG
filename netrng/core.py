""" NetRNG

    A network connected random number generator

    Copyright 2014 Infincia LLC
    
    See LICENSE file for license information

"""

from __future__ import absolute_import, print_function, division

__author__ = 'Stephen Oliver'
__maintainer__ = 'Stephen Oliver <steve@infincia.com>'
__version__ = '0.2a0'
__license__ = 'MIT'
__all__ = [Server, Client]

# standard libraries
import time
import sys
import os
import logging
import msgpack
import errno
import signal

# pip packages
import gevent
import gevent.subprocess
import gevent.queue
import gevent.socket
from gevent.server import StreamServer
from gevent.pool import Pool
from gevent.coros import RLock
from gevent import Timeout
from zeroconf import ServiceBrowser, Zeroconf, ServiceInfo

# delimiter for end of socket messages
SOCKET_DELIMITER = '--NETRNG-SOCKET-DELIMITER'

# library logger
log = logging.getLogger(__name__)


class Server(object):
    '''
        NetRNG server
    
    '''
    
    
    
    
    def __init__(self,
                 listen_address=None,
                 port=None,
                 max_clients=None,
                 sample_size_bytes=None,
                 hwrng_device=None):
        log.info('NetRNG server: initializing')

        # Listen address used by the server
        self.listen_address = listen_address

        # TCP port to listen on
        self.port = port



        # Maximum number of clients to accept, this prevents your HWRNG from being
        # overloaded, starving clients. This requires testing and depends entirely on
        # how fast your HWRNG can be read. A device that can spit out 1mbps (100KB/s) could
        # give 100 clients 1KB/s, but a device that can only generate 128bps may only
        # be able to serve 1 client slowly
        self.max_clients = max_clients

        # How much random data to request from the device for each client push
        self.sample_size_bytes = sample_size_bytes



        # Source device to use for random data, should be something fast and
        # high quality, DON'T set this to /dev/random
        self.hwrng_device = hwrng_device

        # open the hwrng for reading later during client requests
        self.hwrng = open(self.hwrng_device, 'r')




        
        # lock to prevent multiple clients from getting the same random samples
        self.rng_lock = RLock()

        self.zeroconf_controller = Zeroconf()


    def broadcast_service(self):
        desc = {'version': __version__}
        info = ServiceInfo('_netrng._tcp.local.', '{}._netrng._tcp.local.'.format(socket.gethostname()), socket.inet_aton(self.listen_address), self.port, 0, 0, desc)
        log.info('NetRNG server: registering service with Bonjour: %s', info)
        self.zeroconf_controller.registerService(info)

    def unregister_service(self):
        log.info('NetRNG server: unregistering all bonjour services')
        self.zeroconf_controller.unregisterAllServices()

    def serve(self, sock, address):
        '''
            Serves client connections providing random samples to them in a one-to-many 
            request response architecture, with locking to ensure each client gets unique
            samples
    
        '''
        log.debug('NetRNG server: client connected %s', address)

        try:
            while True:
                log.debug('NetRNG server: receive cycle start')
                requestmsg = ""
                with Timeout(3, gevent.Timeout):
                    while True:
                        data = sock.recv(1024)
                        requestmsg = requestmsg + data
                        log.debug('NetRNG server: receive cycle')
                        if SOCKET_DELIMITER in requestmsg:
                            break
                        gevent.sleep()
                requestmsg = requestmsg.replace(SOCKET_DELIMITER, '')
                request = msgpack.unpackb(requestmsg)
                log.debug('NetRNG server: receive cycle done')
                log.debug('NetRNG server: request received %s', request)
                if request['get'] == 'sample':
                    with self.rng_lock:
                        log.debug('NetRNG server: rng lock acquired')
                        sample = self.hwrng.read(self.sample_size_bytes)
                    log.debug('NetRNG server: rng lock released')
                    log.debug('NetRNG server: sending response')
                    responsemsg = msgpack.packb({'push': 'sample', 'sample': sample})
                    sock.sendall(responsemsg + SOCKET_DELIMITER)
                if request['get'] == 'heartbeat':
                    log.debug('NetRNG server: sending heartbeat response to %s', address)
                    responsemsg = msgpack.packb({'push': 'heartbeat'})
                    sock.sendall(responsemsg + SOCKET_DELIMITER)
        except socket.error as e:
            if isinstance(e.args, tuple):
                if e[0] == errno.EPIPE:
                    log.debug('NetRNG server: client disconnected %s', address)
            else:
                log.exception('NetRNG server: socket error %s', e)
        except gevent.Timeout as timeout:
            log.debug('NetRNG server: client socket timeout')
        except Exception as e:
            log.exception('NetRNG server: %s', e)
        finally:
            sock.close()


    def calibrate(self):
        '''
            Naive implementation of auto-calibration for entropy source, should
            check how much entropy can be received in a given number of seconds
            and use that information to decide how much entropy can be distributed
            per second. With that information, it should be possible to decide
            how many clients can be promised `sample_size_bytes` per second

        '''
        log.info('NetRNG server: starting entropy source performance calibration')
        calibration_period = 15 # seconds
        received_entropy = ""
        stop_time = time.time() + calibration_period
        while time.time() < stop_time:
            with self.rng_lock:
                received_entropy += self.hwrng.read(self.sample_size_bytes)
        received_entropy_size = len(received_entropy)
        received_entropy_per_second = received_entropy_size / calibration_period
        log.info('NetRNG server: entropy source can provide %.2f bytes per second', received_entropy_per_second)

    def start(self, use_zeroconf=False):
        '''
            Server starts listening on a TCP socket and spawns a greenlet for each
            new connection. Blocks caller.

        '''
        self.pool = Pool(self.max_clients)
        self.server = StreamServer((self.listen_address, self.port), self.serve, spawn=self.pool)
        log.info('NetRNG server: serving up to %d connections on %s:%d)', self.max_clients, self.listen_address, self.port)
        try:
            self.server.start()
            if use_zeroconf:
                self.broadcast_service()
            gevent.wait()
        except KeyboardInterrupt as e:
            log.debug('NetRNG server: exiting due to keyboard interrupt')
            sys.exit(0)


    def stop(self, use_zeroconf=False):
        '''
            Server stops listening on the TCP socket, stops accepting new connections
            and finally kills spawned connection handlers

        '''
        log.debug('NetRNG server: stopping server and killing existing client connections')
        if use_zeroconf:
            self.unregister_service()
        self.server.stop()







class Client(object):
    '''
        NetRNG client
    
    '''
    def __init__(self, server_address=None, port=None):
        log.info('NetRNG client: initializing')

        # Address of the server to connect to
        self.server_address = server_address
    
        # TCP port to connect to on the server
        self.port = port



        # client socket for connecting to server
        self.rngd = gevent.subprocess.Popen(['rngd','-f','-r','/dev/stdin'],
                                               stdin=gevent.subprocess.PIPE,
                                               stdout=open(os.devnull, 'w'),
                                               stderr=open(os.devnull, 'w'),
                                               close_fds=True)

        # client socket for connecting to server
        self.sock = None
    
        self.zeroconf_controller = Zeroconf()




        # queue for pushing received samples to the rngd subprocess as needed
        self.rngd_queue = gevent.queue.Queue(maxsize=10)


    def rngd_handler(self):
        '''
            Iterates over the rngd_queue

        '''
        log.debug('NetRNG client: starting rngd queue greenlet')
        try:
            while True:
                sample = self.rngd_queue.get()
                self.rngd.stdin.write(sample)
                self.rngd.stdin.flush()
                gevent.sleep()
        except gevent.GreenletExit as exit:
            log.debug('NetRNG client: rngd queue greenlet exiting due to graceful quit')
        except OSError:
            return

    
    def stream(self):
        '''
            Opens a connection to the server, configures the sample size to
            match the server configuration, then starts feeding received samples to rngd 
            running in a subprocess. 
            
            Running rngd in a subprocess allows runtime control over
            starting/stopping/configuring it at the right times

        '''
        log.debug('NetRNG client: starting stream greenlet')

        # client socket for connecting to server
        server_socket = None

        # Connection state
        server_connected = False

        while True:
            try:
                if not server_connected:
                    server_socket = gevent.socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    server_socket.connect((self.server_address, self.port))
                    log.debug('NetRNG client: connected to %s:%d', self.server_address, self.port)
                    server_connected = True



                if self.rngd_queue.full():
                    # send a keepalive to the server
                    log.debug('NetRNG client: sending heartbeat message')
                    requestmsg = msgpack.packb({'get': 'heartbeat'})
                    server_socket.sendall(requestmsg + SOCKET_DELIMITER)
                    log.debug('NetRNG client: heartbeat request sent')
                else:
                    # request a new sample
                    log.debug('NetRNG client: requesting sample')
                    requestmsg = msgpack.packb({'get': 'sample'})
                    server_socket.sendall(requestmsg + SOCKET_DELIMITER)
                    log.debug('NetRNG client: sample request sent')


                # wait for response
                log.debug('NetRNG client: receive cycle start')
                responsemsg = ""
                with Timeout(2, gevent.Timeout):
                    while True:
                        data = server_socket.recv(1024)
                        responsemsg = responsemsg + data
                        log.debug('NetRNG client: receive cycle')
                        if SOCKET_DELIMITER in responsemsg:
                            break
                        gevent.sleep()

                responsemsg = responsemsg.replace(SOCKET_DELIMITER, '')
                response = msgpack.unpackb(responsemsg)
                log.debug('NetRNG client: receive cycle done')


                if response['push'] == 'sample':
                    sample = response['sample']
                    log.debug('NetRNG client: received %d byte sample', len(sample))
                    self.rngd_queue.put(sample)
                elif response['push'] == 'heartbeat':
                    log.debug('NetRNG client: received heartbeat response')
                    gevent.sleep(1)
                else:
                    log.debug('NetRNG client: received unknown response from server')

            except socket.error as socket_exception:
                log.debug('NetRNG client: server unavailable, reconnecting in 10 seconds')
                server_connected = False
                server_socket.close()
                gevent.sleep(10)
            except gevent.Timeout as timeout:
                log.debug('NetRNG client: server socket timeout')
                server_connected = False
                server_socket.close()
                gevent.sleep(1)
            except KeyboardInterrupt as keyboard_exception:
                log.debug('NetRNG client: exiting due to keyboard interrupt')
                server_connected = False
                server_socket.close()
                break
            except gevent.GreenletExit as exit:
                log.debug('NetRNG client: stream greenlet exiting due to graceful quit')
                server_connected = False
                server_socket.close()
                break
            except Exception as unknown_exception:
                log.exception('NetRNG client: unknown exception %s', unknown_exception)
                server_connected = False
                server_socket.close()
        sys.exit(0)


    def start(self, use_zeroconf=False):
        '''
            Client spawns a greenlet for the rngd handler and the network stream
            connection, then joins and waits for them to block the caller

        '''
        log.debug('NetRNG client: spawning greenlets for rngd and stream')
        try:
            rngd_greenlet = gevent.spawn(self.rngd_handler)
            stream_greenlet = gevent.spawn(self.stream)
            greenlets = [stream_greenlet, rngd_greenlet]
            gevent.joinall(greenlets)
        except KeyboardInterrupt as e:
            log.debug('NetRNG client: exiting due to keyboard interrupt')
        finally:
            gevent.killall(greenlets)
            sys.exit(0)