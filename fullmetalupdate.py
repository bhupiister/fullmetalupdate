#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from configparser import ConfigParser
from pathlib import Path
import logging
import argparse
import asyncio
import aiohttp
import ssl
from distutils.util import strtobool
from fullmetalupdate.fullmetalupdate_ddi_client import (
    FullMetalUpdateDDIClient,
    HawkbitManagementClient,
)


class _GatewayAuthSession:
    """
    Wrap an aiohttp.ClientSession and force
    'Authorization: GatewayToken <token>' on every request.
    """

    def __init__(self, session: aiohttp.ClientSession, gateway_token_value: str):
        self._s = session
        self._auth_value = f"GatewayToken {gateway_token_value}"

    def _merge_headers(self, headers):
        h = dict(headers or {})
        # Force override any Authorization header set by downstream code
        h["Authorization"] = self._auth_value
        return h

    # Methods used by DDIClient
    def get(self, url, **kwargs):
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._s.get(url, **kwargs)

    def post(self, url, **kwargs):
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._s.post(url, **kwargs)

    def put(self, url, **kwargs):
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._s.put(url, **kwargs)

    def delete(self, url, **kwargs):
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._s.delete(url, **kwargs)


async def main():
    # ------------------------------------------------------------------
    # Parse CLI args and config file
    # ------------------------------------------------------------------
    config = ConfigParser()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        help="config file",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        default=False,
        help="enable debug mode",
    )

    args = parser.parse_args()

    if not args.config:
        args.config = "config.cfg"

    cfg_path = Path(args.config)

    if not cfg_path.is_file():
        print("Cannot read config file '{}'".format(cfg_path.name))
        exit(1)

    config.read_file(cfg_path.open())

    try:
        LOG_LEVEL = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warn": logging.WARN,
            "error": logging.ERROR,
            "fatal": logging.FATAL,
        }[config.get("client", "log_level").lower()]
    except Exception:
        LOG_LEVEL = logging.INFO

    if args.debug:
        LOG_LEVEL = logging.DEBUG

    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ------------------------------------------------------------------
    # Read config values
    # ------------------------------------------------------------------
    server_host_name_qdn = config.get("server", "server_host_name_qdn")

    OSTREE_SUBDOMAIN = config.get("ostree", "ostree_subdomain")
    HAWKBIT_SUBDOMAIN = config.get("client", "hawkbit_subdomain")

    HOST = HAWKBIT_SUBDOMAIN + "." + server_host_name_qdn
    SSL = config.getboolean("client", "hawkbit_ssl_device")
    TENANT_ID = config.get("client", "hawkbit_tenant_id")
    TARGET_NAME = config.get("client", "hawkbit_target_name")

    ATTRIBUTES = {"FullMetalUpdate": config.get("client", "hawkbit_target_name")}

    DEV_MODE = config.getboolean("dev", "enabled", fallback=False)
    DEV_STATE_DIR = config.get("dev", "state_dir", fallback=None)
    if DEV_STATE_DIR:
        dev_state_path = Path(DEV_STATE_DIR)
        if not dev_state_path.is_absolute():
            dev_state_path = cfg_path.parent / dev_state_path
        DEV_STATE_DIR = str(dev_state_path)

    # Prefer Gateway token; fall back to auth_token only if no gateway token
    try:
        GW_TOKEN = config.get("client", "hawkbit_gateway_token")
    except Exception:
        GW_TOKEN = ""
    AUTH_TOKEN_CFG = config.get("client", "hawkbit_auth_token")

    # mTLS-related paths from config
    hawkbit_ca_cert = config.get("client", "hawkbit_ca_cert", fallback=None)
    hawkbit_client_cert = config.get("client", "hawkbit_client_cert", fallback=None)
    hawkbit_client_key = config.get("client", "hawkbit_client_key", fallback=None)

    if strtobool(config.get("ostree", "ostree_ssl_device")):
        url_type = "https://"
    else:
        url_type = "http://"

    OSTREE_REMOTE_ATTRIBUTES = {
        "name": config.get("ostree", "ostree_name_remote"),
        "gpg-verify": strtobool(config.get("ostree", "ostree_gpg-verify")),
        "url": url_type + OSTREE_SUBDOMAIN + "." + server_host_name_qdn,
        'tls-ca-path': config.get('ostree', 'ostree_ca_cert', fallback=None),
        'tls-client-cert-path': config.get('ostree', 'ostree_client_cert', fallback=None),
        'tls-client-key-path': config.get('ostree', 'ostree_client_key', fallback=None),
    }

    # ------------------------------------------------------------------
    # Build SSL context for hawkBit mTLS if SSL is enabled
    # ------------------------------------------------------------------
    ssl_context = None
    if SSL:
        try:
            # behave like: curl --cacert <hawkbit_ca_cert> --cert <device.crt> --key <device.key>
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

            # Trust store (CA)
            if hawkbit_ca_cert:
                ssl_context.load_verify_locations(cafile=hawkbit_ca_cert)
            else:
                # fallback to system CAs if no custom CA specified
                ssl_context.load_default_certs(ssl.Purpose.SERVER_AUTH)

            # Client certificate + private key (mTLS)
            if hawkbit_client_cert and hawkbit_client_key:
                ssl_context.load_cert_chain(
                    certfile=hawkbit_client_cert,
                    keyfile=hawkbit_client_key,
                )
            else:
                logging.warning(
                    "hawkbit_ssl_device is true, but client cert/key are not configured; "
                    "connection will use server-side TLS only (no mTLS)."
                )

            ssl_context.verify_mode = ssl.CERT_REQUIRED
            ssl_context.check_hostname = True

        except Exception as e:
            logging.error("Failed to create SSL context for Hawkbit mTLS: %s", e)
            exit(1)

    # Create connector; only attach SSL context when we actually use HTTPS to hawkBit
    if ssl_context is not None:
        connector = aiohttp.TCPConnector(ssl=ssl_context)
    else:
        connector = aiohttp.TCPConnector()  # plain HTTP or default TLS

    # ------------------------------------------------------------------
    # Create session (with mTLS) and optionally wrap it to force GatewayToken
    # ------------------------------------------------------------------
    async with aiohttp.ClientSession(connector=connector) as real_session:
        session_for_ddi = real_session

        if GW_TOKEN:
            # Use GatewayToken and prevent DDIClient's own Authorization header from winning
            session_for_ddi = _GatewayAuthSession(real_session, GW_TOKEN)
            AUTH_TOKEN = None
        else:
            # Fall back to old behavior with hawkbit_auth_token (may be None)
            AUTH_TOKEN = AUTH_TOKEN_CFG

        management_client = HawkbitManagementClient(
            real_session,
            HOST,
            SSL,
            AUTH_TOKEN_CFG,
        )

        client = FullMetalUpdateDDIClient(
            session_for_ddi,
            HOST,
            SSL,
            TENANT_ID,
            TARGET_NAME,
            AUTH_TOKEN,
            ATTRIBUTES,
            DEV_MODE,
            DEV_STATE_DIR,
            management_client,
        )

        if not client.init_checkout_existing_containers():
            client.logger.info("There is no containers pre-installed on the target")

        if not client.init_ostree_remotes(OSTREE_REMOTE_ATTRIBUTES):
            client.logger.error(
                "Cannot initialize OSTree remote from config file '{}'".format(
                    cfg_path.name
                )
            )
        else:
            await client.start_polling()


if __name__ == "__main__":
    # create event loop, open aiohttp client session and start polling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
