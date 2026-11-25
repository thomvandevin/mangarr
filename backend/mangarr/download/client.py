"""The torrent download client selected in Settings."""

from .qbittorrent import QbtClient
from .transmission import TransmissionClient


def torrent_client(
    values: dict[str, str], qbittorrent: type[QbtClient] = QbtClient
) -> QbtClient | TransmissionClient:
    """Callers pass the QbtClient name of their own module, which tests replace."""
    client = TransmissionClient if values.get("torrent_client") == "transmission" else qbittorrent
    return client(values["qbittorrent_url"], values["qbittorrent_username"], values["qbittorrent_password"])
