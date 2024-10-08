import base64
from io import BytesIO
from pathlib import Path
from typing import Iterable, Dict, Optional

from os import listdir
from uuid import uuid4
from time import sleep, time
from threading import Thread
from json import loads, dumps
from random import getrandbits
from websocket import WebSocketApp
from requests import Session, post, get
from enum import Enum


class ServerMessage(Enum):
    PING = "2"
    PING_ACK = "3"
    ANON_RESPONSE = "40"
    PENDING = "42"
    RESPONSE = "43"
    UNKNOWN = "6"


class ClientMessage(Enum):
    PING = "2"  # Is this even a valid msg?
    PONG = "3"
    PONG_ACK = "5"
    QUERY = "42"


class Perplexity:
    def __init__(self, email: str) -> None:
        self.session: Session = Session()
        self.user_agent: dict = {
            "User-Agent": "Ask/2.9.1/2406 (iOS; iPhone; Version 17.1) isiOSOnMac/false",
            "X-Client-Name": "Perplexity-iOS",
            "X-App-ApiClient": "ios",
        }
        self.session.headers.update(self.user_agent)

        if ".perplexity_session" in listdir():
            self._recover_session(email)
        else:
            self._init_session_without_login()
            self._login(email)

        self.t: str = self._get_t()
        self.sid: str = self._get_sid()
        self._ask_anonymous_user()

        self.email: str = email
        self.n: int = 0
        self.queue: list = []
        self.finished: bool = True
        self.last_uuid: str = None
        self.backend_uuid: str = None  # unused because we can't yet follow-up questions
        self.frontend_session_id: str = str(uuid4())

        self.ws: WebSocketApp = self._init_websocket()
        self.ws_thread: Thread = Thread(target=self.ws.run_forever).start()
        self._auth_session()

        while not (self.ws.sock and self.ws.sock.connected):
            sleep(0.01)

    def _recover_session(self, email: str) -> None:
        with open(".perplexity_session", "r") as f:
            perplexity_session: dict = loads(f.read())

        if email in perplexity_session:
            self.session.cookies.update(perplexity_session[email])
        else:
            self._login(email, perplexity_session)

    def _login(self, email: str, ps: dict = None) -> None:
        self.session.post(
            url="https://www.perplexity.ai/api/auth/signin-email", data={"email": email}
        )

        email_link: str = str(input("paste the link you received by email: "))
        self.session.get(email_link)

        if ps:
            ps[email] = self.session.cookies.get_dict()
        else:
            ps = {email: self.session.cookies.get_dict()}

        with open(".perplexity_session", "w") as f:
            f.write(dumps(ps))

    def _init_session_without_login(self) -> None:
        self.session.get(url=f"https://www.perplexity.ai/search/{str(uuid4())}")
        self.session.headers.update(self.user_agent)

    def _auth_session(self) -> None:
        self.session.get(url="https://www.perplexity.ai/api/auth/session")

    def _get_t(self) -> str:
        return format(getrandbits(32), "08x")

    def _get_sid(self) -> str:
        return loads(
            self.session.get(
                url=f"https://www.perplexity.ai/socket.io/?EIO=4&transport=polling&t={self.t}"
            ).text[1:]
        )["sid"]

    def _get_cookies_str(self) -> str:
        cookies = ""
        for key, value in self.session.cookies.get_dict().items():
            cookies += f"{key}={value}; "
        return cookies[:-2]

    def _write_file_url(self, filename: str, file_url: str) -> None:
        if ".perplexity_files_url" in listdir():
            with open(".perplexity_files_url", "r") as f:
                perplexity_files_url: dict = loads(f.read())
        else:
            perplexity_files_url: dict = {}

        perplexity_files_url[filename] = file_url

        with open(".perplexity_files_url", "w") as f:
            f.write(dumps(perplexity_files_url))

    def _ask_anonymous_user(self) -> bool:
        response = self.session.post(
            url=f"https://www.perplexity.ai/socket.io/?EIO=4&transport=polling&t={self.t}&sid={self.sid}",
            data='40{"jwt":"anonymous-ask-user"}',
        ).text

        return response == "OK"

    def _init_websocket(self) -> WebSocketApp:
        def on_open(ws: WebSocketApp) -> None:
            ws.send(ClientMessage.PING.value + "probe")
            ws.send(ClientMessage.PONG_ACK.value)

        def on_message(ws: WebSocketApp, message: str) -> None:
            if message == ServerMessage.PING.value:
                ws.send(ClientMessage.PONG.value)
            elif message[0] == ServerMessage.PING_ACK.value and message[1:] == "probe":
                ws.send(ClientMessage.PONG_ACK.value)
            elif message.startswith(ServerMessage.ANON_RESPONSE.value):
                pass
            elif message.startswith(ServerMessage.UNKNOWN.value):
                pass
            elif not self.finished:
                if message.startswith(ServerMessage.PENDING.value):
                    message: list = loads(message[2:])
                    assert message[0] == "query_progress"
                    content: dict = message[1]
                    if "text" in content:
                        content["text"] = loads(content["text"])
                    if (not ("final" in content and content["final"])) or (
                        "status" in content and content["status"] == "completed"
                    ):
                        self.queue.append(content)
                    if message[0] == "query_answered":
                        self.last_uuid = content["uuid"]
                        self.finished = True
                elif message.startswith(ServerMessage.RESPONSE.value + str(self.n)):
                    prefix = ServerMessage.RESPONSE.value + str(self.n)
                    message: dict = loads(message[len(prefix) :])[0]
                    if "text" in message:
                        message["text"] = loads(message["text"])
                    if (
                        "uuid" in message and message["uuid"] != self.last_uuid
                    ) or "uuid" not in message:
                        self.queue.append(message)
                        self.finished = True
                    # Increment n for the next query
                    self.n += 1

                else:
                    raise Exception(f"unhandled message: {message}")
            else:
                raise Exception(f"unhandled message: {message}")

        return WebSocketApp(
            url=f"wss://www.perplexity.ai/socket.io/?EIO=4&transport=websocket&sid={self.sid}",
            header=self.user_agent,
            cookie=self._get_cookies_str(),
            on_open=on_open,
            on_message=on_message,
            on_error=lambda ws, err: print(f"websocket error: {err}"),
        )

    def _s(
        self,
        query: str,
        mode: str = "concise",
        search_focus: str = "internet",
        backend_uuid=False,
        follow_up=None,
        attachments: list[str] = [],
        language: str = "en-GB",
        timezone: str = "America/Chicago",
        in_page: str = None,
        in_domain: str = None,
        is_incognito: bool = False,
    ) -> None:
        """

        Notes:
        - Attachments from followups are not automatically attached to the follow-up question.
        """
        assert self.finished, "already searching"
        assert mode in ["concise", "copilot"], "invalid mode"

        assert len(attachments) <= 4, "too many attachments: max 4"
        assert (
            in_page is None or in_domain is None
        ), "in_page and in_domain can't be used together"
        assert search_focus in [
            "internet",
            "scholar",
            "writing",
            "wolfram",
            "youtube",
            "reddit",
            "reasoning",
        ], "invalid search focus"
        if follow_up is not None:
            assert len(attachments) == 0

        if in_page:
            search_focus = "in_page"
        if in_domain:
            search_focus = "in_domain"

        ws_message: str = f"{ClientMessage.QUERY.value + str(self.n)}" + dumps(
            [
                "perplexity_ask",
                query,
                {
                    "version": "2.13",
                    "source": "default",
                    "last_backend_uuid": backend_uuid,
                    "read_write_token": "",
                    "attachments": attachments,
                    "language": language,
                    "timezone": timezone,
                    "search_focus": search_focus,
                    "frontend_session_id": self.frontend_session_id,
                    "frontend_uuid": str(uuid4()),
                    "mode": mode,
                    # is_related_query, visitor_id, user_nextauth_id, frontend_context_uuid=None|uuid, prompt_source=user, query_source=modal|followup
                    "in_page": search_focus,
                    "is_incognito": is_incognito,
                    "in_domain": in_domain,
                },
            ]
        )

        self._sendquery(ws_message)

    def _sendquery(self, msg: str) -> None:
        assert self.finished, "already searching"
        self.finished = False
        self.ws.send(msg)

    def search(
        self, query: str, timeout: Optional[float] = None, **kwargs
    ) -> Iterable[Dict]:
        self._s(query, **kwargs)

        start_time: float = time()
        while (not self.finished) or len(self.queue) != 0:
            if timeout is not None and time() - start_time > timeout:
                self.finished = True
                return {"error": "timeout"}
            if len(self.queue) != 0:
                yield self.queue.pop(0)

    def search_sync(
        self, query: str, timeout: Optional[float] = None, **kwargs
    ) -> dict:
        self._s(query, **kwargs)

        start_time: float = time()
        while not self.finished:
            if timeout is not None and time() - start_time > timeout:
                self.finished = True
                return {"error": "timeout"}

        return self.queue.pop(-1)

    def upload(self, filename: str) -> str:
        assert self.finished, "already searching"
        content_types = {
            "txt": "text/plain",
            "pdf": "application/pdf",
            "jpg": "image/jpeg",
            "png": "image/png",
        }

        reverse_lookup = {v: k for k, v in content_types.items()}

        if filename.startswith("http"):
            file = get(filename).content
        elif filename.startswith("data:"):
            # Turn the base64 into a file. ignore ugly split code.
            contents = filename.split(",", 1)[1]
            file = BytesIO(base64.b64decode(contents))
            type_ = filename.split(";")[0].split(":")[1]
            filename = str(uuid4()) + "." + reverse_lookup[type_]
        else:
            with open(filename, "rb") as f:
                file = f.read()

        file_ext = Path(filename).suffix[1:]
        assert (
            file_ext in content_types
        ), "invalid file format, must be one of: txt, pdf, jpg, png"
        content_type = content_types[file_ext]

        ws_message: str = f"{ClientMessage.QUERY.value + str(self.n)}" + dumps(
            [
                "get_upload_url",
                {
                    "version": "2.13",
                    "source": "default",
                    "content_type": content_type,
                },
            ]
        )

        self._sendquery(ws_message)
        while not self.finished or len(self.queue) != 0:
            if len(self.queue) != 0:
                upload_data = self.queue.pop(0)

        assert not upload_data["rate_limited"], "rate limited"

        # Convert the fields into files
        fields_as_files = {
            key: (None, value) for key, value in upload_data["fields"].items()
        }
        resp = post(
            url=upload_data["url"],
            files={
                **fields_as_files,
                "file": (filename, file),
            },
        )

        json = resp.json()
        file_url: str = json["secure_url"]

        self._write_file_url(filename, file_url)

        return file_url

    def threads(self, query: str = None, limit: int = None) -> list[dict]:
        assert self.email, "not logged in"
        assert self.finished, "already searching"

        if not limit:
            limit = 20
        data: dict = {
            "version": "2.13",
            "source": "default",
            "limit": limit,
            "offset": 0,
        }
        if query:
            data["search_term"] = query

        ws_message: str = f"{self.base + self.n}" + dumps(["list_ask_threads", data])

        self.ws.send(ws_message)

        while not self.finished or len(self.queue) != 0:
            if len(self.queue) != 0:
                return self.queue.pop(0)

    def list_autosuggest(
        self, query: str = "", search_focus: str = "internet"
    ) -> list[dict]:
        assert self.finished, "already searching"

        ws_message: str = f"{self.base + self.n}" + dumps(
            [
                "list_autosuggest",
                query,
                {
                    "has_attachment": False,
                    "search_focus": search_focus,
                    "source": "default",
                    "version": "2.13",
                },
            ]
        )

        self.ws.send(ws_message)

        while not self.finished or len(self.queue) != 0:
            if len(self.queue) != 0:
                return self.queue.pop(0)

    def close(self) -> None:
        self.ws.close()

        if self.email:
            with open(".perplexity_session", "r") as f:
                perplexity_session: dict = loads(f.read())

            perplexity_session[self.email] = self.session.cookies.get_dict()

            with open(".perplexity_session", "w") as f:
                f.write(dumps(perplexity_session))
