"""Interface de canal pour l'executor notification (BUILD_BRIEF.md commit 5).

Un canal traduit un message de notification en un appel d'outil du fleet MCP
(ToolCall : serveur + outil + arguments). L'execution reelle de l'appel est
deleguee a un ToolInvoker injecte (defini dans notification.py), de sorte que la
logique de canal reste testable sans le fleet.

Canaux fournis : email (imap-mcp), WhatsApp, SMS (twilio). Les identifiants de
serveur MCP sont des constantes a confirmer au branchement du fleet (commit 11).
"""

import abc
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# Identifiants de serveur MCP cote fleet (a confirmer a l'integration, commit 11).
SERVER_EMAIL = "imap"
SERVER_WHATSAPP = "whatsapp"
SERVER_SMS = "twilio"


@dataclass
class ToolCall:
    """Appel d'outil MCP : serveur, outil, arguments."""

    server: str
    tool: str
    arguments: dict


@dataclass
class NotificationMessage:
    """Message de notification projete depuis le payload d'un job."""

    canal: str
    destinataire: Optional[str]
    sujet: Optional[str]
    corps: Optional[str]
    options: dict = field(default_factory=dict)

    # Alias acceptes dans le payload (tolerance de redaction en langage naturel).
    _ALIASES = {
        "canal": ("canal", "channel"),
        "destinataire": ("destinataire", "to", "recipient", "numero", "number", "chat"),
        "sujet": ("sujet", "subject", "objet", "titre"),
        "corps": ("corps", "message", "body", "texte", "text", "contenu"),
        "options": ("options", "args", "arguments"),
    }

    @classmethod
    def from_payload(cls, payload: dict) -> "NotificationMessage":
        def pick(field_name: str):
            for key in cls._ALIASES[field_name]:
                if key in payload and payload[key] not in (None, ""):
                    return payload[key]
            return None

        options = pick("options") or {}
        if not isinstance(options, dict):
            options = {}
        return cls(
            canal=(pick("canal") or "").strip(),
            destinataire=pick("destinataire"),
            sujet=pick("sujet"),
            corps=pick("corps"),
            options=options,
        )


def normalize_channel(name: str) -> str:
    """Normalise le nom de canal (sans accent, minuscule) pour le matching."""
    no_accent = "".join(
        c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c)
    )
    return no_accent.strip().lower()


class Channel(abc.ABC):
    """Interface d'un canal de notification."""

    key: str
    # Noms normalises reconnus pour ce canal.
    aliases: tuple[str, ...] = ()

    @abc.abstractmethod
    def build_call(self, message: NotificationMessage) -> ToolCall:
        """Construit le ToolCall, ou leve ValueError si un champ requis manque."""

    @staticmethod
    def _require(message: NotificationMessage) -> None:
        if not message.destinataire:
            raise ValueError("destinataire manquant pour la notification")
        if not message.corps:
            raise ValueError("corps du message manquant pour la notification")


class EmailChannel(Channel):
    key = "email"
    aliases = ("email", "mail", "imap", "courriel")

    def build_call(self, message: NotificationMessage) -> ToolCall:
        self._require(message)
        arguments = {
            "to": message.destinataire,
            "subject": message.sujet or "(notification)",
            "body": message.corps,
        }
        arguments.update(message.options)
        return ToolCall(SERVER_EMAIL, "imap_send_email", arguments)


class WhatsAppChannel(Channel):
    key = "whatsapp"
    aliases = ("whatsapp", "wa")

    def build_call(self, message: NotificationMessage) -> ToolCall:
        self._require(message)
        arguments = {"recipient": message.destinataire, "message": message.corps}
        arguments.update(message.options)
        return ToolCall(SERVER_WHATSAPP, "send_message", arguments)


class SmsChannel(Channel):
    key = "sms"
    aliases = ("sms", "twilio", "texto")

    def build_call(self, message: NotificationMessage) -> ToolCall:
        self._require(message)
        arguments = {"to": message.destinataire, "body": message.corps}
        arguments.update(message.options)
        return ToolCall(SERVER_SMS, "send_sms", arguments)


def default_channels() -> dict[str, Channel]:
    """Registre canal normalise -> instance, alias compris."""
    registry: dict[str, Channel] = {}
    for channel in (EmailChannel(), WhatsAppChannel(), SmsChannel()):
        for alias in channel.aliases:
            registry[normalize_channel(alias)] = channel
    return registry
