import subprocess

import requests

from seleniumwire.proxy.client import AdminClient
from seleniumwire.proxy.handler import CaptureRequestHandler
from seleniumwire.proxy.server import ProxyHTTPServer

client = AdminClient()

proxy = 'https://username:password@server:port'

seleniumwire_options = dict(
    standalone=False,
    # verify_ssl=False,
    suppress_connection_errors=False,
)
proxy = {
    'http': proxy,
    'https': proxy,
    'no_proxy': ','.join({
        'localhost',
        '127.0.0.1',
    }),
}
addr, port = client.create_proxy(
# proxy = client.create_proxy(
    port=12345,
    proxy_config=proxy,
    options=seleniumwire_options
)

http = False
https = True

if http:
    response = requests.get('http://ifconfig.me', proxies=dict(
        http='http://localhost:12345',
        https='http://localhost:12345',
    ))

    print(response.text)

if https:
    for insecure in [True, False]:
        print(f'#### insecure {insecure} ####')
        command = ['curl',
                   # '-vvv',
                   '--max-time', '1',
                   '--insecure' if insecure else '',
                   # '--proxy-insecure',
                   '--proxy', 'http://localhost:12345',
                   'https://ifconfig.me']
        print(' '.join(command))
        # subprocess.run(command)
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, _ = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()

        out = out.decode('utf-8')
        print(out)
        continue
        if '502' in out:
            for line in out.split('\n'):
                if 'explanation' in line:
                    print(line.split('<p>')[-1].split('</p>')[0])
                    break
            else:
                print(repr(out))
        else:
            print(repr(out))


# import time
# time.sleep(10)

    # response = requests.get('https://ifconfig.me', proxies=dict(
    #     http='http://localhost:12345',
    #     https='http://localhost:12345',
    # ), verify=False)
    #
    # print(response.text)

# from seleniumwire import webdriver  # Import from seleniumwire
# seleniumwire_options['proxy'] = proxy
# driver = webdriver.Chrome(seleniumwire_options=seleniumwire_options)
# driver.get('https://www.google.com')
#
# # Access requests via the `requests` attribute
# for request in driver.requests:
#     if request.response:
#         print(
#             request.path,
#             request.response.status_code,
#             request.response.headers['Content-Type']
#         )
#
