"""Module for sending data and hosting sending servers."""


from aiohttp import web
import asyncio
import humanize
import magic
from multiprocessing import Process
import os
import pyqrcode
import requests
import socket
import tempfile
from time import sleep, strftime
from zeroconf import IPVersion, ServiceInfo, Zeroconf
from zipfile import ZipFile


from .utils import get_local_ip_address, get_zip_file


__all__ = ["send", "send_server", "send_server_proc"]


# Request handlers


async def _text_sender(request):
    """Returns the text being shared, GET handler for route '/'."""
    address = ""
    peername = request.transport.get_extra_info("peername")
    if peername is not None:
        host, _ = peername
        address = " (by " + str(host) + ")"
    print("Content requested" + address + ", transferred!")
    return web.Response(text=request.app["text"])


async def _download_page(request):
    """Renders a download page, GET handler for route '/'."""
    file_name = request.app["file_name"]
    file_size = humanize.naturalsize(request.app["file_size"])
    return web.Response(text="""
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="UTF-8">
        <title>Airshare Download</title>
        </head>
        <body>
        <form action="/download" method="get">
            <input type="submit" value="Download {} ({})"/>
        </form>
        </body>
        </html>
    """.format(file_name, file_size), content_type="text/html")


async def _file_stream_sender(request):
    """Streams a file from the server, GET handler for route '/download'."""
    address = ""
    peername = request.transport.get_extra_info("peername")
    if peername is not None:
        host, _ = peername
        address = " (by " + str(host) + ")"
    print("Content requested" + address + ", transferred!")
    response = web.StreamResponse()
    file_path = request.app["file_path"]
    file_name = request.app["file_name"]
    file_size = str(request.app["file_size"])
    header = "attachment; filename={}; size={}".format(file_name, file_size)
    response.headers["content-type"] = magic.Magic(mime=True) \
                                            .from_file(file_path)
    response.headers["content-length"] = str(request.app["file_size"])
    response.headers["content-disposition"] = header
    await response.prepare(request)
    with open(file_path, "rb") as f:
        chunk = f.read(8192)
        while chunk:
            await response.write(chunk)
            chunk = f.read(8192)
    return response


async def _is_airshare_text_sender(request):
    """Returns 'Text Sender', GET handler for route '/airshare'."""
    return web.Response(text="Text Sender")


async def _is_airshare_file_sender(request):
    """Returns 'File Sender', GET handler for route '/airshare'."""
    return web.Response(text="File Sender")


# Sender functions


def send(*, code, file, compress=False):
    r"""Send file(s) or directories to a receiving server.

    Parameters
    ----------
    code : str
        Identifying code for the Airshare receiving server.
    file : str or list or None
        Relative path or list of paths of the files or directories to serve.
        For multiple files or directories, contents are automatically zipped.
    compress : boolean, default=False
        Flag to enable or disable compression (Zip).
        Effective when only one file is given.

    Returns
    -------
    Returns 0 if successful.
    Returns 1 on failure.
    """
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    service = "_airshare._http._tcp.local."
    info = zeroconf.get_service_info(service, code + service)
    if info is None:
        print("The airshare `" + code + ".local` does not exist!")
        return 1
    if type(file) is str:
        if file == "":
            file = None
        else:
            file = [file]
    elif len(file) == 0:
        file = None
    if file is None:
        raise ValueError("The parameter `file` must be non-empty!")
    if compress or len(file) > 1 or os.path.isdir(file[0]):
        file, name = get_zip_file(file)
    else:
        file, name = file[0], file[0].split(os.path.sep)[-1]
    ip = socket.inet_ntoa(info.addresses[0])
    url = "http://" + ip + ":" + str(info.port)
    airshare_type = requests.get(url + "/airshare")
    if airshare_type.text != "Upload Receiver":
        print("The airshare `" + code + ".local` is not an upload receiver!")
        return 1
    file_form = {"upload_file": (name, open(file, "rb"))}
    requests.post(url + "/upload", files=file_form)
    print("Uploaded `" + name + "` to airshare `" + code + ".local`!")
    return 0


def send_server(*, code, text=None, file=None, compress=False, port=80):
    r"""Serves a file or text and registers it as a Multicast-DNS service.

    Parameters
    ----------
    code : str
        Identifying code for the Airshare service and server.
    text : str or None
        String value to be shared.
        If both `text` and `files` are given, `text` will be shared.
        Must be given if `files` is not given.
    file : str or list or None
        Relative path or list of paths of the files or directories to serve. If
        multiple files or directories are given, the contents are automatically
        zipped. If not given or both `files` and `text` are given, `text` will
        be shared. Must be given if `text` is not given.
    compress : boolean, default=False
        Flag to enable or disable compression (Zip).
        Effective when only one file is given.
    port : int, default=80
        Port number at which the server is hosted on the device.
    """
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    service = "_airshare._http._tcp.local."
    info = zeroconf.get_service_info(service, code + service)
    if info is not None:
        raise ValueError("`" + code
                         + "` already exists, please use a different code!")
    if file is not None:
        if type(file) is str:
            if file == "":
                file = None
            else:
                file = [file]
        elif len(file) == 0:
            file = None
    content = text or file
    name = None
    if content is None:
        raise ValueError("Either `file` or `text` (keyword arguments) must be"
                         + " given and non-empty!")
    elif text is None and file is not None:
        if compress or len(file) > 1 or os.path.isdir(file[0]):
            content, name = get_zip_file(file)
        else:
            content = file[0]
    addresses = [get_local_ip_address()]
    info = ServiceInfo(
        service,
        code + service,
        addresses=addresses,
        port=port,
        server=code + ".local."
    )
    zeroconf.register_service(info)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    file_size = ""
    if text is not None:
        app["text"] = content
        app.router.add_get(path="/", handler=_text_sender)
        app.router.add_get(path="/airshare", handler=_is_airshare_text_sender)
    elif file:
        app["file_path"] = os.path.realpath(content)
        app["file_name"] = name or app["file_path"].split(os.path.sep)[-1]
        app["file_size"] = os.stat(app["file_path"]).st_size
        file_size = " (" + humanize.naturalsize(app["file_size"]) + ") "
        content = app["file_name"]
        app.router.add_get(path="/", handler=_download_page)
        app.router.add_get(path="/airshare", handler=_is_airshare_file_sender)
        app.router.add_get(path="/download", handler=_file_stream_sender)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", str(port))
    loop.run_until_complete(site.start())
    url_port = ""
    if port != 80:
        url_port = ":" + str(port)
    ip = socket.inet_ntoa(info.addresses[0])
    print("`" + content + "`" + file_size + "available at " + ip + url_port
          + " and `http://" + code + ".local" + url_port + "`, press CtrlC"
          + " to stop sharing...")
    print(pyqrcode.create("http://" + ip + url_port).terminal(quiet_zone=1))
    loop.run_forever()


def send_server_proc(*, code, text=None, file=None, compress=False, port=80):
    r"""Creates a process with 'send_server' as the target.

    Parameters
    ----------
    code : str
        Identifying code for the Airshare service and server.
    text : str or None
        String value to be shared.
        If both `text` and `files` are given, `text` will be shared.
        Must be given if `files` is not given.
    file : str or list or None
        Relative path or list of paths of the files or directories to serve. If
        multiple files or directories are given, the contents are automatically
        zipped. If not given or both `files` and `text` are given, `text` will
        be shared. Must be given if `text` is not given.
    compress : boolean, default=False
        Flag to enable or disable compression (Zip).
        Effective when only one file is given.
    port : int, default=80
        Port number at which the server is hosted on the device.

    Returns
    -------
    process: multiprocessing.Process
        A multiprocessing.Process object with 'send_server' as target.
    """
    kwargs = {"code": code, "file": file, "text": text, "compress": compress,
              "port": port}
    process = Process(target=send_server, kwargs=kwargs)
    return process
