"""Executable entry point: ``python -m jarvis`` (also exposed as the ``jarvis`` script)."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from jarvis import __version__
from jarvis.app.application import Application
from jarvis.app.chat_service import ChatService
from jarvis.app.demo import run_voice_demo
from jarvis.app.disk_service import DiskService
from jarvis.app.system_service import SystemService
from jarvis.app.voice_service import LoopBuilder, VoiceService
from jarvis.asr import AsrService, AsrSettings
from jarvis.config import ConfigService
from jarvis.core.exceptions import JarvisError
from jarvis.llm import LlmService
from jarvis.logging import LOG_FORMAT, LoggingService, LoggingSettings
from jarvis.orchestration import OrchestrationService, OrchestrationSettings, PipelineEvent
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tts import TtsService, TtsSettings
from jarvis.vad import VadService, VadSettings
from jarvis.wakeword import WakeWordService, WakeWordSettings

logger = logging.getLogger("jarvis.main")


def _configure_bootstrap_logging(*, verbose: bool) -> None:
    """Minimal console logging, alive only until ``LoggingService`` starts.

    Anything logged before/while components start (including startup
    failures of ``ConfigService`` itself) must still be visible; once
    ``LoggingService.start()`` runs it replaces these handlers with the
    real console/file setup.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command line interface for the JARVIS launcher."""
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="JARVIS - a voice-first, agentic Windows AI assistant.",
    )
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help="explicit user config file (overrides JARVIS_CONFIG and the default location)",
    )
    parser.add_argument(
        "--wav",
        type=Path,
        default=None,
        metavar="FILE",
        help="offline demo: run one 16 kHz mono WAV through VAD -> ASR -> LLM -> TTS and exit",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="FILE",
        help="with --wav: reply WAV to write (default: <input>_reply.wav)",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="with --wav: echo the transcript back instead of calling the model",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="open the desktop HUD (needs pip install -e '.[desktop]')",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="with --desktop: allow the HUD to open the microphone when you press 启用语音",
    )
    return parser


class _VoiceStack:
    """ASR + TTS + the pipeline, as one start/stop unit.

    Exists so :class:`~jarvis.app.voice_service.VoiceService` can build and tear
    down roughly 3 GB of models on a background thread without knowing anything
    about the engines involved.
    """

    def __init__(self, services: list[object], orchestration: OrchestrationService) -> None:
        self._services = services
        self._orchestration = orchestration

    def start(self) -> None:
        for service in self._services:
            starter = getattr(service, "start", None)
            if callable(starter):
                starter()
        self._orchestration.start()

    def stop(self) -> None:
        self._orchestration.stop()
        for service in reversed(self._services):
            stopper = getattr(service, "stop", None)
            if callable(stopper):
                stopper()

    @property
    def listening(self) -> bool:
        return self._orchestration.listening


def _voice_stack_builder(
    config_service: ConfigService,
    llm_service: LlmService,
) -> LoopBuilder:
    """Return the callable that loads the voice models when the page asks.

    The config sections are read at call time and forced enabled. Their ``enabled``
    flags gate model loading inside the services, and a desktop user pressing a
    button has not edited ``asr.enabled`` -- the consent was the button, resolved
    here by the composition root rather than by a silent default.
    """

    def build(on_event: Callable[[PipelineEvent], None]) -> _VoiceStack:
        config = config_service.config

        def report(event: PipelineEvent) -> None:
            # Console first, then the caller's sink: if the UI path ever throws, the
            # milestone is still in the log. A voice chain observable only through
            # its own window is undebuggable the moment that window hangs.
            _report_pipeline_event(event)
            on_event(event)

        asr = AsrService(lambda: AsrSettings(section=dataclasses.replace(config.asr, enabled=True)))
        tts = TtsService(lambda: TtsSettings(section=dataclasses.replace(config.tts, enabled=True)))
        orchestration = OrchestrationService(
            lambda: OrchestrationSettings(
                section=dataclasses.replace(config.orchestration, enabled=True),
                wakeword=config.wakeword,
                vad=config.vad,
            ),
            asr=asr,
            tts=tts,
            llm_client_provider=lambda: llm_service.client,
            on_event=report,
        )
        return _VoiceStack([asr, tts], orchestration)

    return build


def _run_desktop(args: argparse.Namespace) -> int:
    """Start the services and hand them to the desktop HUD window."""
    app = Application()
    config_service = ConfigService(config_file=args.config)
    app.register(config_service)
    app.register(
        LoggingService(
            lambda: LoggingSettings(
                section=config_service.config.logging,
                logs_dir=config_service.paths.logs_dir,
            )
        )
    )
    # The UI never touches psutil directly; it reads this L4 service instead.
    system = SystemService(lambda: SystemMonitor(top_processes=10))
    app.register(system)
    # JARVIS' own data root sits under %LOCALAPPDATA% like any other cache, so the
    # cleaner is told explicitly to leave it alone -- otherwise "clean my temp files"
    # could sweep away the assistant's models and the audit trail proving it.
    disk = DiskService(
        lambda: DiskCleaner(
            audit_log=config_service.paths.audit_dir / "deletions.jsonl",
            protected_dirs=(config_service.paths.data_dir,),
        )
    )
    app.register(disk)
    # A model client is cheap to start and needs only an API key, so typed
    # questions can be answered the moment the window opens -- no microphone, and
    # no gigabytes of speech models.
    llm_service = LlmService(lambda: config_service.config.llm)
    app.register(llm_service)
    chat = ChatService(lambda: llm_service.client)
    app.register(chat)
    # Deliberately *not* registered here: WakeWordService and VadService each run
    # their own capture loop, and a second one would fight the voice pipeline for
    # the same microphone. Mic exclusivity is enforced by what this list contains,
    # not by a config value somebody could set twice.
    voice = VoiceService(
        _voice_stack_builder(config_service, llm_service),
        permission=lambda: args.voice or config_service.config.orchestration.enabled,
        keywords=lambda: config_service.config.wakeword.keywords,
    )
    app.register(voice)

    try:
        app.start()
    except Exception:
        logger.exception("desktop mode failed to start")
        return 1
    try:
        from jarvis.ui import desktop

        # -v doubles as "show me the page's console": WebView2 devtools are the
        # only way to see what the bridge actually answered.
        return desktop.run(system, disk, voice=voice, chat=chat, debug=args.verbose)
    finally:
        app.stop()


def _run_demo(args: argparse.Namespace) -> int:
    """One-shot offline demo run; returns a process exit code."""
    config_service = ConfigService(config_file=args.config)
    try:
        config_service.start()
    except Exception:
        logger.exception("configuration failed to load")
        return 1
    try:
        result = run_voice_demo(
            config_service.config,
            args.wav,
            out_path=args.out,
            skip_llm=args.skip_llm,
        )
    except JarvisError as exc:
        logger.error("demo run failed: %s", exc)
        print(f"演示失败：{exc}", file=sys.stderr)
        return 1
    except Exception:
        logger.exception("demo run failed")
        return 1
    finally:
        config_service.stop()

    print(f"检测到 {result.utterances} 段语音")
    print(f"转写：{result.transcript}")
    echoed = "（已跳过模型，原样朗读）" if result.llm_skipped else ""
    print(f"回答{echoed}：{result.reply}")
    if result.audio_path is not None:
        print(f"语音回复已写入：{result.audio_path}（{result.audio_sample_rate} Hz）")
    else:
        print("语音回复：无音频产出", file=sys.stderr)
    return 0


_PIPELINE_EVENT_LABELS = {
    "wake": "唤醒",
    "speech_start": "听到说话",
    "speech_end": "识别中",
    "reply": "回答",
    "barge_in": "被打断",
    "error": "出错",
}


def _report_pipeline_event(event: PipelineEvent) -> None:
    """Surface every voice-pipeline milestone on the console.

    Without this the assistant looks idle whenever the wake word scores just
    under threshold — and "not listening" is indistinguishable from "listening
    and unheard".
    """
    label = _PIPELINE_EVENT_LABELS.get(event.kind, event.kind)
    level = logging.ERROR if event.kind == "error" else logging.INFO
    score = f" (score={event.detail:.3f})" if isinstance(event.detail, float) else ""
    logger.log(level, "[%s] %s%s", label, event.text, score)


def _run_until_interrupt(app: Application) -> int:
    """Block so the capture threads actually get to exist."""
    print("语音链路已就绪：说「Hey Jarvis」唤醒，Ctrl+C 退出。", file=sys.stderr)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        logger.info("received Ctrl+C; shutting down")
    finally:
        app.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the application and return a process exit code."""
    args = build_arg_parser().parse_args(argv)
    _configure_bootstrap_logging(verbose=args.verbose)

    if args.wav is not None:
        return _run_demo(args)
    if args.desktop:
        return _run_desktop(args)

    app = Application()
    config_service = ConfigService(config_file=args.config)
    app.register(config_service)

    def logging_settings() -> LoggingSettings:
        section = config_service.config.logging
        if args.verbose and section.level != "DEBUG":
            # -v wins over configuration: a debugging run must be verbose.
            section = dataclasses.replace(section, level="DEBUG")
        return LoggingSettings(section=section, logs_dir=config_service.paths.logs_dir)

    app.register(LoggingService(logging_settings))
    llm_service = LlmService(lambda: config_service.config.llm)
    app.register(llm_service)
    # NOTE: the standalone wake-word / VAD loops are registered *only* in the
    # ``else`` branch below — they are mutually exclusive with the orchestration
    # service (which drives the same engines internally over one mic loop).
    asr_service = AsrService(lambda: AsrSettings(section=config_service.config.asr))
    app.register(asr_service)
    tts_service = TtsService(lambda: TtsSettings(section=config_service.config.tts))
    app.register(tts_service)

    def orch_settings() -> OrchestrationSettings:
        return OrchestrationSettings(
            section=config_service.config.orchestration,
            wakeword=config_service.config.wakeword,
            vad=config_service.config.vad,
        )

    # All providers are lazy: config is read only inside each component's
    # start(), after ConfigService has loaded it. OrchestrationService and the
    # standalone wake-word / VAD services each self-gate on their own
    # ``enabled`` flag, so the mic-exclusivity contract is enforced by the
    # configuration (orchestration mode requires wakeword/vad left disabled).
    app.register(
        OrchestrationService(
            orch_settings,
            asr=asr_service,
            tts=tts_service,
            llm_client_provider=lambda: llm_service.client,
            on_event=_report_pipeline_event,
        )
    )
    app.register(
        WakeWordService(
            lambda: WakeWordSettings(
                section=config_service.config.wakeword,
                environ=dict(os.environ),
            )
        )
    )
    app.register(
        VadService(
            lambda: VadSettings(
                section=config_service.config.vad,
                sample_rate=16_000,
            )
        )
    )
    try:
        app.start()
    except Exception:
        logger.exception("application failed to start")
        return 1

    logger.info(
        "jarvis %s initialized (env=%s, data dir: %s, %d component(s) registered)",
        __version__,
        config_service.config.app.environment,
        config_service.paths.data_dir,
        len(app.components),
    )

    if config_service.config.orchestration.enabled:
        # The voice pipeline runs on its own capture threads. Returning here
        # would tear the microphone down in the same instant it opened.
        return _run_until_interrupt(app)
    app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
