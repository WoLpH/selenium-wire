# -*- coding: utf-8 -*-

#
#
# This code is from the project https://github.com/inaz2/proxy2, with some
# minor modifications.
#
#

import base64
import re
import socket
import ssl
import sys
import threading
import urllib.parse
from functools import partial

import OpenSSL.crypto
import tlslite
from socket import error as SocketError, timeout as SocketTimeout
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from . import cert, socks


def tlslite_getpeercert(conn):
    if not hasattr(conn, '_peercert'):
        x509_bytes = conn.session.serverCertChain.x509List[0].bytes
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_ASN1,
                                               bytes(x509_bytes))
        subject = x509.get_subject()
        abbvs = {
            'CN': 'commonName',
            'L': 'localityName',
            'ST': 'stateOrProvinceName',
            'O': 'organizationName',
            'OU': 'organizationalUnitName',
            'C': 'countryName',
            'STREET': 'streetAddress',
            'DC': 'domainComponent',
            'UID': 'userid',
        }
        cert = {}
        cert['subject'] = [[(abbvs.get(k.decode()) or k.decode(), v.decode())
                            for k, v in subject.get_components()]]
        for i in range(x509.get_extension_count()):
            extension = x509.get_extension(i)
            if extension.get_short_name() == b'subjectAltName':
                cert['subjectAltName'] = []
                for p in extension.get_data().split(b'\x82')[1:]:
                    cert['subjectAltName'].append(('DNS', p[1:].decode()))
        conn._peercert = cert
    return conn._peercert


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def handle_error(self, request, client_address):
        # surpress socket/ssl related errors
        cls, e = sys.exc_info()[:2]
        print('got error', request, client_address, cls, e)
        return HTTPServer.handle_error(self, request, client_address)
        if issubclass(cls, socket.error) or issubclass(cls, ssl.SSLError):
            pass
        else:
            return HTTPServer.handle_error(self, request, client_address)


class ProxyRequestHandler(BaseHTTPRequestHandler):
    admin_path = 'http://proxy2'
    # Path to the directory used to store the generated certificates.
    # Subclasses can override certdir
    certdir = cert.CERTDIR

    def __init__(self, request, client_address, server):
        self.tls = threading.local()
        self.tls.conns = {}
        self.websocket = False

        super().__init__(request, client_address, server)

    def do_CONNECT(self):
        print('do_CONNECT', self.path)
        self.send_response(200, 'Connection Established')
        self.end_headers()

        hostname = self.path.split(':')[0]
        certfile = cert.generate(hostname, self.certdir)
        keyfile = cert.CERTKEY

        # check_hostname = False
        # context = ssl._create_default_https_context()
        # context.check_hostname = check_hostname
        # context.verify_mode = ssl.CERT_REQUIRED
        # if keyfile or certfile:
        #     context.load_cert_chain(certfile, keyfile)
        #
        # context.load_verify_locations(cert.CACERT)
        #
        # # with ssl.wrap_socket(self.connection, keyfile=cert.CERTKEY, certfile=certpath, server_side=True) as conn:
        # print('connection', self.connection)
        # conn = context.wrap_socket(self.connection, server_side=True)
        with ssl.wrap_socket(self.connection, keyfile=cert.CERTKEY,
                             certfile=certfile, server_side=True) as conn:
            self.connection = conn
            self.rfile = conn.makefile('rb', self.rbufsize)
            self.wfile = conn.makefile('wb', self.wbufsize)

        print('wrapped', self.connection)
        conntype = self.headers.get('Proxy-Connection', '')
        if self.protocol_version == 'HTTP/1.1' and conntype.lower() != 'close':
            self.close_connection = False
        else:
            self.close_connection = True

    def do_GET(self):
        # import traceback
        # traceback.print_stack()
        print('do_GET', self.path)

        if self.path.startswith(self.admin_path):
            self.admin_handler()
            return
        req = self
        content_length = int(req.headers.get('Content-Length', 0))
        req_body = self.rfile.read(content_length) if content_length else None

        if req.path[0] == '/':
            path = '{}{}'.format(req.headers['Host'], req.path)
            if isinstance(self.connection, ssl.SSLSocket):
                req.path = 'https://{}'.format(path)
            else:
                req.path = 'http://{}'.format(path)

        req_body_modified = self.request_handler(req, req_body)
        if req_body_modified is False:
            self.send_error(403)
            return
        elif req_body_modified is not None:
            req_body = req_body_modified
            del req.headers['Content-length']
            req.headers['Content-length'] = str(len(req_body))

        u = urllib.parse.urlsplit(req.path)
        scheme, netloc, path = u.scheme, u.netloc, (u.path + '?' + u.query if u.query else u.path)
        assert scheme in ('http', 'https')
        if netloc:
            req.headers['Host'] = netloc
        setattr(req, 'headers', self.filter_headers(req.headers))

        origin = (scheme, netloc)
        conn = None
        try:
            conn = self.create_connection(origin)
            conn.request(self.command, path, req_body, dict(req.headers))
            res = conn.getresponse()

            if res.headers.get('Upgrade') == 'websocket':
                self.websocket = True

            version_table = {10: 'HTTP/1.0', 11: 'HTTP/1.1'}
            setattr(res, 'headers', res.msg)
            setattr(res, 'response_version', version_table[res.version])

            res_body = res.read()
        except Exception:
            import traceback
            traceback.print_exc()
            if origin in self.tls.conns:
                del self.tls.conns[origin]
            self.send_error(502)
            return
        finally:
            if conn and not self.websocket:
                conn.close()

        res_body_modified = self.response_handler(req, req_body, res, res_body)
        if res_body_modified is False:
            self.send_error(403)
            return
        elif res_body_modified is not None:
            res_body = res_body_modified
            del res.headers['Content-length']
            res.headers['Content-Length'] = str(len(res_body))

        setattr(res, 'headers', self.filter_headers(res.headers))

        self.send_response(res.status, res.reason)

        for header, val in res.headers.items():
            self.send_header(header, val)
        self.end_headers()

        if res_body:
            self.wfile.write(res_body)

        self.wfile.flush()

        if self.websocket:
            self.handle_websocket(conn.sock)
        else:
            self.close_connection = True

    def create_connection(self, origin):
        scheme, netloc = origin

        if origin not in self.tls.conns:
            proxy_config = self.server.proxy_config
            if proxy_config and proxy_config.get(scheme):
                proxy_scheme = proxy_config[scheme].scheme
            else:
                proxy_scheme = ''

            kwargs = {
                'timeout': self.timeout
            }

            if scheme == 'https' and proxy_scheme != 'https':
            # if scheme == 'https':
                connection = ProxyAwareHTTPSConnection
                if not self.server.options.get('verify_ssl', True):
                    kwargs['context'] = ssl._create_unverified_context()
                # self.tls.conns[origin] = connection
            else:
                # kwargs['context'] = ssl._create_unverified_context()
                connection = ProxyAwareHTTPConnection

            print(connection, proxy_config, netloc, kwargs)
            self.tls.conns[origin] = connection(proxy_config, netloc, **kwargs)

        return self.tls.conns[origin]

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET
    do_PATCH = do_GET

    def filter_headers(self, headers):
        # http://tools.ietf.org/html/rfc2616#section-13.5.1
        hop_by_hop = (
            'keep-alive',
            'proxy-authenticate',
            'proxy-authorization',
            'te',
            'trailers',
            'transfer-encoding',
        )

        for k in hop_by_hop:
            del headers[k]

        # Remove the `connection` header for non-websocket requests
        if 'connection' in headers:
            if 'upgrade' not in headers['connection'].lower():
                del headers['connection']

        # Accept only supported encodings
        if 'Accept-Encoding' in headers:
            ae = headers['Accept-Encoding']

            if self.server.options.get('disable_encoding') is True:
                permitted_encodings = ('identity', )
            else:
                permitted_encodings = ('identity', 'gzip', 'x-gzip', 'deflate')

            filtered_encodings = [x for x in re.split(r',\s*', ae) if x in permitted_encodings]

            if not filtered_encodings:
                filtered_encodings.append('identity')

            del headers['Accept-Encoding']

            headers['Accept-Encoding'] = ', '.join(filtered_encodings)

        return headers

    def handle_one_request(self):
        if not self.websocket:
            super().handle_one_request()

    def handle_websocket(self, server_sock):
        self.connection.settimeout(None)
        server_sock.settimeout(None)

        def server_read():
            try:
                while True:
                    serverdata = server_sock.recv(4096)
                    if not serverdata:
                        break
                    self.connection.sendall(serverdata)
            finally:
                if server_sock:
                    server_sock.close()
                if self.connection:
                    self.connection.close()

        t = threading.Thread(target=server_read, daemon=True)
        t.start()

        try:
            while True:
                clientdata = self.connection.recv(4096)
                if not clientdata:
                    break
                server_sock.sendall(clientdata)
        finally:
            if server_sock:
                server_sock.close()
            if self.connection:
                self.connection.close()

        t.join()

    def send_cacert(self):
        with open(cert.CACERT, 'rb') as f:
            data = f.read()

        self.send_response(200, 'OK')
        self.send_header('Content-Type', 'application/x-x509-ca-cert')
        self.send_header('Content-Length', len(data))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)

    def request_handler(self, req, req_body):
        pass

    def response_handler(self, req, req_body, res, res_body):
        pass

    def admin_handler(self):
        if self.path == 'http://proxy2.test/':
            self.send_cacert()

    def log_error(self, format_, *args):
        # suppress "Request timed out: timeout('timed out',)"
        if isinstance(args[0], socket.timeout):
            return

        self.log_message(format_, *args)


class ProxyAwareHTTPConnection(HTTPConnection):
    """A specialised HTTPConnection that will transparently connect to a
    HTTP or SOCKS proxy server based on supplied proxy configuration.
    """

    def __init__(self, proxy_config, netloc, *args, **kwargs):
        self.proxy_config = proxy_config
        self.netloc = netloc
        self.use_proxy = 'http' in proxy_config and netloc not in proxy_config.get('no_proxy', '')

        if self.use_proxy and proxy_config['http'].scheme.startswith('http'):
            self.custom_authorization = proxy_config.get('custom_authorization')
            super().__init__(proxy_config['http'].hostport, *args, **kwargs)
        else:
            super().__init__(netloc, *args, **kwargs)

    def _setup_https_tunnel(self):
        sock = self.sock

        host = self._tunnel_host
        port = self._tunnel_port

        try:
            lines = []
            lines.append('CONNECT %s:%d HTTP/1.1' % (host, port))
            lines.append('Host: %s:%d' % (host, port))

            if self._tunnel_headers:
                for item in self._tunnel_headers.items():
                    lines.append('%s: %s' % item)

            data = '\r\n'.join(lines) + '\r\n\r\n'
            sock.sendall(data.encode())

            data = b''
            code = 0
            pos = -1
            while True:
                s = sock.recv(4096)
                if not s:
                    if code == 0:
                        raise SocketError("Tunnel connection failed: %r" % data)
                    break
                data += s
                if code == 0 and b'\r\n' in data:
                    version, code, message = data.split(b' ', 2)
                    if code != b'200':
                        sock.close()
                        raise SocketError("Tunnel connection failed: %s %s" %
                                          (code, message.strip()))
                pos = data.find(b'\r\n\r\n')
                if pos > 0:
                    break

            tls_conn = tlslite.TLSConnection(sock)
            try:
                tls_conn.handshakeClientCert(serverName=host)
            except Exception:
                sock.close()
                raise

            try:
                ssl.match_hostname(tlslite_getpeercert(tls_conn), host)
            except Exception:
                tls_conn.close()
                raise
        except SocketTimeout as e:
            raise TimeoutError(
                self, "Connection to %s timed out. (connect timeout=%s)" %
                      (self.host, self.timeout))

        except SocketError as e:
            raise SocketError(
                self, "Failed to establish a new connection: %s" % e)

        # patch fileno,
        # let urllib3.util.connection.is_connection_dropped work as expected
        tls_conn.fileno = partial(self._origin_sock.fileno)
        # patch getpeercert
        tls_conn.getpeercert = partial(tlslite_getpeercert, tls_conn)
        self.sock = tls_conn

    def connect(self):
        print('connect')
        proxy_config = self.proxy_config.get('https')
        proxy_scheme = proxy_config.scheme if proxy_config else ''
        if self.use_proxy and proxy_scheme.startswith('socks'):
            self.sock = _socks_connection(
                self.host,
                self.port,
                self.timeout,
                proxy_config
            )
        elif self.use_proxy and proxy_scheme == 'https':
            # connection = HTTPSConnection(self.host, check_hostname=True)
            # connection.connect()
            # self.sock = connection.sock
            #
            # return self.sock

            sock = self._create_connection(
                (self.host, 443), self.timeout, self.source_address)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            context = ssl.create_default_context()

            self.sock = context.wrap_socket(sock, server_hostname=self.host)

            self._setup_https_tunnel()
            print('created tunnel')

            return
            # context = ssl._create_unverified_context(check_hostname=False)
            # print('host', self.host)
            # self.sock = _https_connection(proxy_config)
            # self.sock = _https_connection(self.proxy_config['http'], context)

            protocol, username, password, hostname, port = parse_proxy(proxy_config)

            context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context = ssl._create_unverified_context(check_hostname=False)

            # certpath = cert.generate(hostname, cert.CERTDIR)
            #     with ssl.wrap_socket(connection.sock,
            #                          keyfile=cert.CERTKEY,
            #                          certfile=certpath, server_side=True) as sock:

            print(hostname, port, self.netloc)
            connection = HTTPSConnection(hostname, port, context=context)
            connection.connect()
            self.sock = connection.sock
            # certpath = cert.generate(hostname, cert.CERTDIR)
            # try:
            #     with ssl.wrap_socket(connection.sock,
            #                          keyfile=cert.CERTKEY,
            #                          certfile=certpath, server_side=True) as sock:
            #         self.sock = sock
            # except Exception as e:
            #     print('got excpeiton', e)
            #     raise
            if self._tunnel_host:
                self._tunnel()
        else:
            super().connect()

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        if headers is None:
            headers = {}

        if self.use_proxy and self.proxy_config['http'].scheme.startswith('http'):
            if not url.startswith('http'):
                url = 'http://{}{}'.format(self.netloc, url)

            headers.update(_create_auth_header(
                self.proxy_config['http'].username,
                self.proxy_config['http'].password,
                self.custom_authorization)
            )

        super().request(method, url, body, headers=headers)


class ProxyAwareHTTPSConnection(HTTPSConnection):
    """A specialised HTTPSConnection that will transparently connect to a
    HTTP or SOCKS proxy server based on supplied proxy configuration.
    """

    def __init__(self, proxy_config, netloc, *args, **kwargs):
        self.proxy_config = proxy_config
        self.use_proxy = 'https' in proxy_config and netloc not in proxy_config.get('no_proxy', '')

        if self.use_proxy and proxy_config['https'].scheme.startswith('http'):
            # For HTTP proxies, CONNECT tunnelling is used
            super().__init__(proxy_config['https'].hostport, *args, **kwargs)
            netloc_host, netloc_port = get_hostname_port(netloc, 'https')
            self.set_tunnel(
                netloc_host,
                netloc_port,
                headers=_create_auth_header(
                    proxy_config['https'].username,
                    proxy_config['https'].password,
                    proxy_config.get('custom_authorization')
                )
            )
        else:
            super().__init__(netloc, *args, **kwargs)

    def connect(self):
        proxy_config = self.proxy_config.get('https')
        proxy_scheme = proxy_config.scheme if proxy_config else ''
        if self.use_proxy and proxy_scheme.startswith('socks'):
            self.sock = _socks_connection(
                self.host,
                self.port,
                self.timeout,
                self.proxy_config['https']
            )
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        # elif self.use_proxy and proxy_scheme == 'https':
            # self.sock = _https_connection(self.proxy_config['https'])
            # self.sock = self._context.wrap_socket(
            #     self.sock, server_hostname=self.host)
            # if self._tunnel_host:
            #     self._tunnel()
        else:
            super().connect()


def _create_auth_header(proxy_username, proxy_password, custom_proxy_authorization):
    """Create the Proxy-Authorization header based on the supplied username
    and password or custom Proxy-Authorization header value.

    Args:
        proxy_username: The proxy username.
        proxy_password: The proxy password.
        custom_proxy_authorization: The custom proxy authorization.
    Returns:
        A dictionary containing the Proxy-Authorization header or an empty
        dictionary if the username or password were not set.
    """
    headers = {}

    if proxy_username and proxy_password and not custom_proxy_authorization:
        proxy_username = urllib.parse.unquote(proxy_username)
        proxy_password = urllib.parse.unquote(proxy_password)
        auth = '{}:{}'.format(proxy_username, proxy_password)
        headers['Proxy-Authorization'] = 'Basic {}'.format(base64.b64encode(auth.encode('utf-8')).decode('utf-8'))
    elif custom_proxy_authorization:
        headers['Proxy-Authorization'] = custom_proxy_authorization

    return headers


def _socks_connection(host, port, timeout, socks_config):
    """Create a SOCKS connection based on the supplied configuration."""
    try:
        socks_type = dict(
            socks4=socks.PROXY_TYPE_SOCKS4,
            socks5=socks.PROXY_TYPE_SOCKS5,
            socks5h=socks.PROXY_TYPE_SOCKS5
        )[socks_config.scheme]
    except KeyError:
        raise TypeError('Invalid SOCKS scheme: {}'.format(socks_config.scheme))

    socks_host, socks_port = socks_config.hostport.split(':')

    return socks.create_connection(
        (host, port),
        timeout,
        None,
        socks_type,
        socks_host,
        int(socks_port),
        socks_config.scheme == 'socks5h',
        socks_config.username,
        socks_config.password,
        ((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),)
    )


def _https_connection(proxy, context=None):
    protocol, username, password, hostname, port = parse_proxy(proxy)

    if not context:
        context = ssl._create_unverified_context()

    connection = HTTPSConnection(hostname, port, context=context)
    connection.connect()
    return connection.sock


def get_hostname_port(host, protocol='http'):
    host = host.split(':', 1)
    hostname = host[0]

    if host[1:]:
        port = int(host[1])
    elif protocol == 'http':
        port = 80
    elif protocol == 'https':
        port = 443
    else:
        port = None

    return hostname, port


def parse_proxy(proxy_config):
    '''Split proxy_config to include correct port
    Expects output from urllib.request._parse_proxy`
    '''
    protocol, username, password, host = proxy_config
    hostname, port = get_hostname_port(host, protocol)

    return protocol, username, password, hostname, port