"""The download clients selected in Settings: one for torrents, SABnzbd for usenet."""

from .qbittorrent import QbtClient
from .sabnzbd import NZB_ID_PREFIX, SabnzbdClient
from .transmission import TransmissionClient


def torrent_client(
    values: dict[str, str], qbittorrent: type[QbtClient] = QbtClient
) -> QbtClient | TransmissionClient:
    """Callers pass the QbtClient name of their own module, which tests replace."""
    client = TransmissionClient if values.get("torrent_client") == "transmission" else qbittorrent
    return client(values["qbittorrent_url"], values["qbittorrent_username"], values["qbittorrent_password"])


def usenet_client(values: dict[str, str]) -> SabnzbdClient:
    return SabnzbdClient(values["sabnzbd_url"], values["sabnzbd_api_key"])


def is_usenet(download_id: str) -> bool:
    """Usenet downloads keep SABnzbd's job id where torrents keep their info hash."""
    return download_id.startswith(NZB_ID_PREFIX)


def torrents_enabled(values: dict[str, str]) -> bool:
    return values.get("qbittorrent_enabled") == "true"


def usenet_enabled(values: dict[str, str]) -> bool:
    return values.get("sabnzbd_enabled") == "true"


def client_enabled(values: dict[str, str], usenet: bool) -> bool:
    return usenet_enabled(values) if usenet else torrents_enabled(values)


def download_client(
    values: dict[str, str], download_id: str, qbittorrent: type[QbtClient] = QbtClient
) -> QbtClient | TransmissionClient | SabnzbdClient:
    return usenet_client(values) if is_usenet(download_id) else torrent_client(values, qbittorrent)
