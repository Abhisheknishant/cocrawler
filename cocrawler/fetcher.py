#        ClientError
#            ClientConnectionError -- socket-related stuff
#                ClientOSError(ClientConnectionError, builtins.OSError) -- errno is set
#                ClientTimeoutError(ClientConnectionError, concurrent.futures._base.TimeoutError)
#                FingerprintMismatch -- SSL-related
#                ProxyConnectionError -- opening connection to proxy
#            ClientHttpProcessingError
#                ClientRequestError -- connection error during sending request
#                ClientResponseError -- connection error during reading response
#        DisconnectedError
#            ClientDisconnectedError
#                WSClientDisconnectedError -- deprecated
#            ServerDisconnectedError
#        HttpProcessingError
#            BadHttpMessage -- 400
#                BadStatusLine "200 OK"
#                HttpBadRequest -- 400
#                InvalidHeader
#                LineTooLong
#            HttpMethodNotAllowed -- 405
#            HttpProxyError -- anything other than success starting to talk to the proxy
#            WSServerHandshakeError -- websocket-related

'''
async fetching of urls.

Assumes robots checks have already been done.

Supports proxies and server mocking.

Success returns response object (caller must release()) and response
bytes (which were already read in order to shake out all potential
errors.)

Failure returns enough details for the caller to do something smart:
503, other 5xx, DNS fail, connect timeout, error between connect and
full response, proxy failure. Plus an errorstring good enough for logging.
'''

import time
#import traceback

import asyncio
import logging
import aiohttp

import stats

LOGGER = logging.getLogger(__name__)

# XXX should be a policy plugin
def apply_url_policies(url, parts, config):
    headers = {}
    proxy = None
    mock_url = None
    mock_robots = None

    test_host = config['Testing'].get('TestHostmapAll')
    if test_host:
        headers['Host'] = parts.netloc
        mock_url = parts._replace(netloc=test_host).geturl()
        mock_robots = parts.scheme + '://' + test_host + '/robots.txt'

    return headers, proxy, mock_url, mock_robots

async def fetch(url, session, config, headers=None, proxy=None, mock_url=None, allow_redirects=None):

    maxsubtries = int(config['Crawl']['MaxSubTries'])
    pagetimeout = float(config['Crawl']['PageTimeout'])
    retrytimeout = float(config['Crawl']['RetryTimeout'])

    if proxy: # pragma: no cover
        proxy = aiohttp.ProxyConnector(proxy=proxy)
        # XXX we need to preserve the existing connector config (see cocrawler.__init__ for conn_kwargs)
        # XXX we should rotate proxies every fetch in case some are borked
        # XXX use proxy history to decide not to use some
        raise ValueError('not yet implemented')

    subtries = 0
    last_exception = None
    response = None

    while subtries < maxsubtries: # XXX make this sub-loop configurable

        try:
            t0 = time.time()
            last_exception = None

            with aiohttp.Timeout(pagetimeout):
                response = await session.get(mock_url or url,
                                             allow_redirects=allow_redirects, headers=headers)

            # XXX special sleepy 503 handling here - soft fail
            # XXX json_log tries
            # XXX serverdisconnected is a soft fail
            # XXX aiodns.error.DNSError
            # XXX equivalent to requests.exceptions.SSLerror ??
            #   reddit.com is an example of a CDN-related SSL fail
            # XXX when we retry, if local_addr was a list, switch to a different IP
            #   (change out the TCPConnector)
            # XXX what does a proxy error look like?
            # XXX record proxy error

            # fully receive headers and body.
            # XXX if we want to limit bytecount, do it here?
            # XXX needs a try/except block because it can throw a subset of the exceptions listed at top
            body_bytes = await response.read()
            header_bytes = response.raw_headers
            apparent_elapsed = '{:.3f}'.format(time.time() - t0)

            # break only if we succeeded. 5xx = fail
            if response.status < 500:
                break

        except Exception as e:
            last_exception = repr(e)
            #traceback.print_exc()
            LOGGER.debug('we sub-failed once, url is %s, exception is %s',
                         mock_url or url, last_exception)

        # treat all 5xx somewhat similar to a 503: slow down and retry
        await asyncio.sleep(retrytimeout)
        # XXX record 5xx so that everyone else slows down, too
        if response:
            # if the exception was thrown during reading body_bytes, there will be a response object
            await response.release()
        subtries += 1
    else:
        if last_exception:
            LOGGER.debug('we failed, the last exception is %s', last_exception)
            return None, None, None, None, last_exception
        return response, body_bytes, header_bytes, apparent_elapsed, None

    stats.stats_sum('URLs fetched', 1)
    LOGGER.debug('url %r came back with status %r', url, response.status)
    stats.stats_sum('fetch http code=' + str(response.status), 1)

    # fish dns for host out of tcpconnector object? requires (host, port)
    #print('on the way out, connector.cached_hosts is', session.connector.cached_hosts)

    # checks after fetch:
    # hsts? if ssl, check strict-transport-security header,
    #   remember max-age=foo part., other stuff like includeSubDomains
    # did we receive cookies? was the security bit set?
    # record everything needed for warc. all headers, for example.


    return response, body_bytes, header_bytes, apparent_elapsed, None

def upgrade_scheme(url):
    '''
    Upgrade crawled scheme to https, if reasonable. This helps to reduce MITM attacks
    against the crawler.
    https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json

    Alternately, the return headers from a site might have strict-transport-security set ... a bit more
    dangerous as we'd have to respect the timeout to avoid permanently learning something that's broken

    TODO: use HTTPSEverwhere? would have to have a fallback if https failed, which it occasionally will
    '''
    return url