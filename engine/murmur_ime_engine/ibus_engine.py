"""A pass-through Python IBus engine that owns inline voice preedit."""

# ruff: noqa: E402 -- GI versions must be selected before repository imports.

from __future__ import annotations

import logging

import gi

gi.require_version("IBus", "1.0")
from gi.repository import GLib, IBus  # noqa: E402

from .constants import ENGINE_NAME
from .policy import (
    is_private_input,
    is_real_input_client,
    valid_preedit_text,
    valid_surrounding_text,
    valid_utterance_id,
)
from .registry import EngineRegistry
from .session import (
    OBSERVATION_TIMEOUT_SECONDS,
    ObservationResult,
    SessionGuard,
)

logger = logging.getLogger(__name__)


class MurmurEngine(IBus.Engine):
    """Voice-only development engine; ordinary keys pass to the application."""

    def __init__(
        self,
        bus: IBus.Bus,
        object_path: str,
        registry: EngineRegistry,
    ) -> None:
        super().__init__(
            connection=bus.get_connection(),
            object_path=object_path,
            engine_name=ENGINE_NAME,
            has_focus_id=True,
            active_surrounding_text=True,
        )
        self._registry = registry
        self._sessions = SessionGuard()
        self._enabled = False
        self._focused = False
        self._focus_context = ""
        self._focus_client = ""
        self._focus_token = 0
        self._capabilities = 0
        self._purpose = int(IBus.InputPurpose.FREE_FORM)
        self._hints = int(IBus.InputHints.NONE)
        self._surrounding_revision = 0
        self._observation_timeout_source_id = 0
        registry.register(self)

    @property
    def active_owner(self) -> str | None:
        return self._sessions.owner

    @property
    def has_active_session(self) -> bool:
        return self._sessions.active is not None

    def can_acquire(self) -> bool:
        return (
            self._enabled
            and self._focused
            and is_real_input_client(self._focus_client)
            and not is_private_input(self._purpose, self._hints)
            and bool(self._capabilities & int(IBus.Capabilite.PREEDIT_TEXT))
        )

    def acquire(self, owner: str, utterance_id: str) -> bool:
        if not valid_utterance_id(utterance_id) or not self.can_acquire():
            return False
        accepted = self._sessions.acquire(owner, utterance_id, self._focus_token)
        if accepted:
            logger.info("Voice preedit acquired at focus token %d", self._focus_token)
        return accepted

    def partial(
        self,
        owner: str,
        utterance_id: str,
        revision: int,
        text: str,
    ) -> bool:
        if not self.can_acquire() or not valid_preedit_text(text):
            return False
        if not self._sessions.accept_text(
            owner,
            utterance_id,
            self._focus_token,
            revision,
            final=False,
        ):
            return False
        self._set_preedit(text)
        logger.debug(
            "Accepted voice partial revision=%d characters=%d",
            revision,
            len(text),
        )
        return True

    def final(
        self,
        owner: str,
        utterance_id: str,
        revision: int,
        text: str,
    ) -> bool:
        if not self.can_acquire() or not valid_preedit_text(text):
            return False
        if not self._sessions.accept_text(
            owner,
            utterance_id,
            self._focus_token,
            revision,
            final=True,
        ):
            return False
        if not self._sessions.begin_observation(
            owner,
            utterance_id,
            self._focus_token,
            surrounding_revision=self._surrounding_revision,
            final_text=text,
            supported=bool(self._capabilities & int(IBus.Capabilite.SURROUNDING_TEXT)),
        ):
            self._sessions.finish()
            self._registry.invalidated(self)
            return False
        self._arm_observation_timeout(owner, utterance_id, self._focus_token)
        self._clear_preedit()
        if text:
            self.commit_text(IBus.Text.new_from_string(text))
        logger.info(
            "Committed voice final revision=%d characters=%d; observation pending",
            revision,
            len(text),
        )
        return True

    def finish_observation(
        self,
        owner: str,
        utterance_id: str,
    ) -> ObservationResult:
        result = self._sessions.finish_observation(
            owner,
            utterance_id,
            self._focus_token,
        )
        if result.consumed:
            self._cancel_observation_timeout()
            self._clear_surrounding_cache()
            self._registry.invalidated(self)
            logger.info(
                "Finished post-commit observation (available=%s)",
                result.accepted,
            )
        return result

    def observation_supported(self, owner: str, utterance_id: str) -> bool:
        """Report capability only for the exact active focus-bound session."""

        return self._sessions.observation_supported(
            owner,
            utterance_id,
            self._focus_token,
        )

    def cancel(self, owner: str, utterance_id: str) -> bool:
        if not self._sessions.cancel(owner, utterance_id):
            return False
        self._cancel_observation_timeout()
        self._clear_surrounding_cache()
        self._clear_preedit()
        self._registry.invalidated(self)
        logger.info("Cancelled voice preedit")
        return True

    def cancel_owner(self, owner: str) -> bool:
        session = self._sessions.active
        if session is None or session.owner != owner:
            return False
        self._cancel_observation_timeout()
        self._sessions.invalidate()
        self._clear_surrounding_cache()
        self._clear_preedit()
        self._registry.invalidated(self)
        logger.info("Cancelled voice preedit after D-Bus owner disappeared")
        return True

    def _set_preedit(self, text: str) -> None:
        self.update_preedit_text_with_mode(
            IBus.Text.new_from_string(text),
            len(text),
            bool(text),
            IBus.PreeditFocusMode.CLEAR,
        )

    def _clear_preedit(self) -> None:
        self.update_preedit_text_with_mode(
            IBus.Text.new_from_string(""),
            0,
            False,
            IBus.PreeditFocusMode.CLEAR,
        )

    def _invalidate_voice(self, reason: str) -> None:
        self._cancel_observation_timeout()
        had_session = self._sessions.invalidate()
        self._clear_surrounding_cache()
        self._clear_preedit()
        self._registry.invalidated(self)
        if had_session:
            logger.info("Invalidated voice preedit: %s", reason)

    def _clear_surrounding_cache(self) -> None:
        self._surrounding_revision += 1
        if isinstance(self, IBus.Engine):
            try:
                IBus.Engine.do_set_surrounding_text(
                    self,
                    IBus.Text.new_from_string(""),
                    0,
                    0,
                )
            except Exception:
                logger.warning("Could not clear IBus surrounding-text cache")

    def _cancel_observation_timeout(self) -> None:
        source_id = getattr(self, "_observation_timeout_source_id", 0)
        if source_id:
            GLib.source_remove(source_id)
            self._observation_timeout_source_id = 0

    def _arm_observation_timeout(
        self,
        owner: str,
        utterance_id: str,
        focus_token: int,
    ) -> None:
        self._cancel_observation_timeout()
        delay_ms = int(OBSERVATION_TIMEOUT_SECONDS * 1_000) + 1
        self._observation_timeout_source_id = GLib.timeout_add(
            delay_ms,
            self._on_observation_timeout,
            owner,
            utterance_id,
            focus_token,
        )

    def _on_observation_timeout(
        self,
        owner: str,
        utterance_id: str,
        focus_token: int,
    ) -> bool:
        self._observation_timeout_source_id = 0
        if self._sessions.expire_observation(owner, utterance_id, focus_token):
            self._clear_surrounding_cache()
            self._registry.invalidated(self)
            logger.info("Expired post-commit observation")
        return GLib.SOURCE_REMOVE

    def _cache_surrounding_text(
        self,
        text: object,
        cursor: int,
        anchor: int,
    ) -> bool:
        """Cache one bounded update and offer it to the active observation."""

        self._surrounding_revision += 1
        if not valid_surrounding_text(text, cursor, anchor):
            self._sessions.update_surrounding(
                self._focus_token,
                self._surrounding_revision,
                None,
            )
            return False
        self._sessions.update_surrounding(
            self._focus_token,
            self._surrounding_revision,
            text,
            cursor,
            anchor,
        )
        return True

    # IBus virtual methods -------------------------------------------------

    def do_process_key_event(self, keyval: int, keycode: int, state: int) -> bool:
        # This prototype proves inline voice preedit.  It intentionally does
        # not pretend to be Rime: returning False leaves ordinary keys alone.
        return False

    def do_focus_in(self) -> None:
        self._focus_in("", "")

    def do_focus_in_id(self, object_path: str, client: str) -> None:
        self._focus_in(object_path, client)

    def _focus_in(self, object_path: str, client: str) -> None:
        if self.has_active_session:
            self._invalidate_voice("focus changed")
        self._focus_token += 1
        self._clear_surrounding_cache()
        self._focus_context = object_path
        self._focus_client = client
        self._focused = is_real_input_client(client)
        logger.debug("Focus in token=%d client=%s", self._focus_token, client)

    def do_focus_out(self) -> None:
        self._focus_out("")

    def do_focus_out_id(self, object_path: str) -> None:
        self._focus_out(object_path)

    def _focus_out(self, object_path: str) -> None:
        if object_path and self._focus_context and object_path != self._focus_context:
            return
        self._focused = False
        self._focus_context = ""
        self._focus_client = ""
        self._focus_token += 1
        self._clear_surrounding_cache()
        self._invalidate_voice("focus lost")

    def do_enable(self) -> None:
        self._enabled = True
        # Besides returning the current cache, this call tells older IBus
        # clients that this engine requires future surrounding-text updates.
        self.get_surrounding_text()

    def do_disable(self) -> None:
        self._enabled = False
        self._focus_token += 1
        self._clear_surrounding_cache()
        self._invalidate_voice("engine disabled")

    def do_reset(self) -> None:
        if self._sessions.observing:
            # GTK resets the active IM context before ordinary Backspace/type
            # edits. The focus token and conservative surrounding-span checks
            # remain the authority during the short post-commit lease.
            getter = getattr(self, "get_surrounding_text", None)
            if callable(getter):
                getter()
            return
        self._focus_token += 1
        self._invalidate_voice("input context reset")

    def do_set_capabilities(self, capabilities: int) -> None:
        self._capabilities = int(capabilities)
        if not self._capabilities & int(IBus.Capabilite.PREEDIT_TEXT):
            self._invalidate_voice("client has no preedit capability")

    def do_set_surrounding_text(
        self,
        text: IBus.Text,
        cursor_pos: int,
        anchor_pos: int,
    ) -> None:
        if is_private_input(self._purpose, self._hints):
            self._clear_surrounding_cache()
            return
        # The parent cache is useful only for the short active observation and
        # is cleared at every completion/invalidation boundary.
        if self._sessions.observing:
            IBus.Engine.do_set_surrounding_text(self, text, cursor_pos, anchor_pos)
        try:
            value: object = text.get_text()
        except Exception:
            value = None
        self._cache_surrounding_text(value, int(cursor_pos), int(anchor_pos))

    def do_set_content_type(self, purpose: int, hints: int) -> None:
        self._purpose = int(purpose)
        self._hints = int(hints)
        if is_private_input(self._purpose, self._hints):
            self._clear_surrounding_cache()
            self._invalidate_voice("private input field")

    def do_destroy(self) -> None:
        self._enabled = False
        self._focused = False
        # PyGObject may invoke destroy on an object whose constructor failed
        # before our Python fields were installed (for example, a missing IBus
        # property on an unsupported runtime). Teardown must remain harmless.
        if hasattr(self, "_sessions"):
            self._invalidate_voice("engine destroyed")
        registry = getattr(self, "_registry", None)
        if registry is not None:
            registry.unregister(self)
        super().destroy()


class MurmurFactory(IBus.Factory):
    """Creates voice engine instances for IBus input contexts."""

    def __init__(self, bus: IBus.Bus, registry: EngineRegistry) -> None:
        super().__init__(connection=bus.get_connection(), object_path=IBus.PATH_FACTORY)
        self._bus = bus
        self._registry = registry
        self._next_engine_id = 0

    def do_create_engine(self, engine_name: str) -> MurmurEngine:
        if engine_name != ENGINE_NAME:
            raise ValueError(f"Unknown engine: {engine_name}")
        object_path = f"/org/murmur/IME/Engine/{self._next_engine_id}"
        self._next_engine_id += 1
        return MurmurEngine(self._bus, object_path, self._registry)
