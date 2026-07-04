from __future__ import annotations

import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager


class ArcaLegacyTlsAdapter(HTTPAdapter):
    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        return context

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs
    ) -> None:
        pool_kwargs["ssl_context"] = self._ssl_context()
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )

    def proxy_manager_for(self, proxy: str, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._ssl_context()
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def build_arca_session(environment: str) -> requests.Session:
    session = requests.Session()
    if environment == "prod":
        session.mount("https://servicios1.afip.gov.ar/", ArcaLegacyTlsAdapter())
    return session
