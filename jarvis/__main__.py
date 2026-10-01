"""Executable entry point: ``python -m jarvis`` (also exposed as the ``jarvis`` script)."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jarvis import __version__
from jarvis.app.announcer import Announcer
from jarvis.app.application import Application
from jarvis.app.chat_service import ChatService
from jarvis.app.command_access import CommandAccess
from jarvis.app.computer_access import ComputerAccess
from jarvis.app.demo import run_voice_demo
from jarvis.app.disk_service import DiskService
from jarvis.app.preferences import Preferences
from jarvis.app.process_service import ProcessService
from jarvis.app.reminder_service import ReminderService
from jarvis.app.settings_service import SettingsService
from jarvis.app.system_service import SystemService
from jarvis.app.transcript_service import TranscriptService
from jarvis.app.usage_service import UsageService
from jarvis.app.voice_picker import VoicePicker
from jarvis.app.voice_service import LoopBuilder, VoiceService
from jarvis.asr import AsrService, AsrSettings
from jarvis.browser import BrowserService
from jarvis.computer import ComputerService
from jarvis.config import AppPaths, ConfigService
from jarvis.core.events import VoicePhase
from jarvis.core.exceptions import JarvisError
from jarvis.database import SqliteStore
from jarvis.knowledge import KnowledgeService
from jarvis.llm import LlmService
from jarvis.logging import LOG_FORMAT, LoggingService, LoggingSettings
from jarvis.mcp import McpService
from jarvis.memory import MemoryScope, MemoryService
from jarvis.ocr import OcrService
from jarvis.orchestration import OrchestrationService, OrchestrationSettings, PipelineEvent
from jarvis.orchestration.player import AudioPlayer, BridgePlayer
from jarvis.planner import PlannerService
from jarvis.plugins import PluginService
from jarvis.prompt import PromptService
from jarvis.scheduler import SchedulerService
from jarvis.tools import ToolService
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.process_control import ProcessController
from jarvis.tts import TtsService, TtsSettings
from jarvis.vad import VadService, VadSettings
from jarvis.vector import VectorService
from jarvis.vision import VisionService
from jarvis.wakeword import WakeWordService, WakeWordSettings
from jarvis.workflow import WorkflowService

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
        "--no-tray",
        action="store_true",
        help=(
            "with --desktop: no notification-area icon, and the X quits like it used "
            "to. The tray is what lets the app keep listening after the window is "
            "closed, so turning it off also turns that off -- mostly useful when "
            "driving the window from a test or a screen recorder"
        ),
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="with --desktop: allow the HUD to open the microphone when you press 启用语音",
    )
    parser.add_argument(
        "--speak",
        type=str,
        default=None,
        metavar="TEXT",
        help=(
            "with --desktop: read TEXT aloud once the voice stack is up, then stay out of the way. "
            "An acceptance aid -- it exercises the whole spoken-answer chain (synthesis, the audio "
            "bridge, the page's own playback) without needing a model key or a microphone"
        ),
    )
    parser.add_argument(
        "--ingest",
        type=Path,
        default=None,
        metavar="PATH",
        help="add a file or a folder to the knowledge base, then exit",
    )
    parser.add_argument(
        "--ask",
        type=str,
        default=None,
        metavar="TEXT",
        help="answer one question from the command line (memory + tools + knowledge base)",
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="list the registered tools with their risk level, then exit",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="show what the assistant has remembered, then exit",
    )
    parser.add_argument(
        "--forget",
        action="store_true",
        help="with --memory: erase everything the assistant has remembered (asks first)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --ingest: re-embed even when the file content is unchanged",
    )
    parser.add_argument(
        "--prompts",
        action="store_true",
        help="list every prompt template with its version and variables, then exit",
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=None,
        metavar="GOAL",
        help="break a goal into steps and print the plan (needs a model key), then exit",
    )
    return parser


@dataclass(frozen=True, slots=True)
class _Capabilities:
    """The services the command-line entry points need to reach.

    Returned rather than re-created, because building this stack opens the
    database and registers tools; doing it twice in one process would register
    every tool twice and open a second connection to the same file.
    """

    database: SqliteStore
    announcer: Announcer
    reminders: ReminderService
    usage: UsageService
    vector: VectorService
    memory: MemoryService
    knowledge: KnowledgeService
    tools: ToolService
    scheduler: SchedulerService
    workflow: WorkflowService
    plugins: PluginService
    prompts: PromptService
    planner: PlannerService


def _logging_settings_factory(
    config_service: ConfigService, *, verbose: bool
) -> Callable[[], LoggingSettings]:
    """Build the ``LoggingService`` settings provider, honouring ``-v``.

    A factory rather than the settings themselves: configuration does not exist
    yet at registration time.

    ``-v`` forces DEBUG. It used to apply only to the non-desktop path, so
    ``jarvis --desktop -v`` opened DevTools but left the log at INFO — which is
    exactly backwards for someone trying to find out why the window is blank,
    because the interesting records are the DEBUG ones.
    """

    def settings() -> LoggingSettings:
        section = config_service.config.logging
        if verbose and section.level != "DEBUG":
            section = dataclasses.replace(section, level="DEBUG")
        return LoggingSettings(section=section, logs_dir=config_service.paths.logs_dir)

    return settings


def _tool_action_runner(tools: ToolService) -> Callable[[str, Mapping[str, object]], str]:
    """Adapt the tool registry into the plain callable the scheduler wants.

    The scheduler and the workflow engine both need "run this named action",
    and neither is allowed to import ``jarvis.tools`` — ``scheduler`` sits at L3
    above ``database`` only, and the dependency table is machine-checked. An
    injected callable is also what lets their tests pass a fake.
    """

    def run(action: str, arguments: Mapping[str, object]) -> str:
        result = tools.invoke(action, dict(arguments))
        if not result.ok:
            # Raising is how the caller records a failed run; returning the
            # message would look like success with a strange output.
            raise JarvisError(result.error or f"动作 {action} 执行失败")
        return result.output

    return run


def _register_capabilities(
    app: Application,
    config_service: ConfigService,
    llm_service: LlmService,
    computer_access: ComputerAccess | None = None,
    command_access: CommandAccess | None = None,
) -> _Capabilities:
    """Register every phase-11-to-18 service on ``app``, in dependency order.

    Order matters and is not alphabetical: ``database`` must be started before
    anything that migrates a table into it, and ``vector`` before ``memory`` and
    ``knowledge``, which index through it. Each service self-gates on its own
    ``enabled`` flag, so registering all of them is the honest default — a
    disabled feature reports itself as disabled rather than being absent.

    Requires ``config_service.start()`` to have run already: constructing the
    database needs a resolved path, and the path is only known once the data
    root has been decided.
    """
    paths: AppPaths = config_service.paths

    # First, because everything below reads prompts: ``render_prompt`` resolves
    # against the registry this service fills in, so an operator override in
    # ``prompt.overrides`` has to be in place before any agent is constructed.
    prompts = PromptService(lambda: config_service.config.prompt)
    app.register(prompts)

    database = SqliteStore(paths.database_dir / config_service.config.database.filename)
    app.register(database)
    # The token ledger. Registered here rather than in the desktop branch because
    # every entry point that talks to a model should be counting, and the one
    # thing that must not happen is the statistics popup showing a number that no
    # code ever produced.
    usage = UsageService(database)
    app.register(usage)
    llm_service.attach_usage_sink(usage.record)

    vector = VectorService(database, lambda: config_service.config.vector)
    app.register(vector)

    memory = MemoryService(
        database,
        vector,
        lambda: config_service.config.memory,
        llm_provider=lambda: llm_service.client,
    )
    app.register(memory)

    knowledge = KnowledgeService(
        database,
        vector,
        lambda: config_service.config.knowledge,
        llm_provider=lambda: llm_service.client,
    )
    app.register(knowledge)

    # Built before the tool service, not after it: the mouse and keyboard tools
    # are only registered when a desktop-control service exists to back them, and
    # advertising a capability the registry cannot reach is how a model ends up
    # promising the operator something that will only ever be refused.
    computer = ComputerService(
        computer_access.section if computer_access else lambda: config_service.config.computer
    )
    # Both of these exist before the tool service because the tools are what expose
    # them: speak and the three reminder tools are registered only when there is
    # something behind them. The announcer starts unbound -- neither the microphone
    # nor the tray exists yet -- and says so rather than pretending it spoke.
    announcer = Announcer()
    reminders = ReminderService(lambda: scheduler)
    tools = ToolService(
        lambda: config_service.config.tools,
        monitor_factory=lambda: SystemMonitor(top_processes=10),
        cleaner_factory=lambda: DiskCleaner(
            audit_log=paths.audit_dir / "deletions.jsonl",
            protected_dirs=(paths.data_dir,),
        ),
        computer_factory=lambda: computer,
        # Same rule as the desktop tools one line above: ``run_powershell`` exists
        # only when there is a level to read it from. A CLI run that never built a
        # CommandAccess gets no PowerShell tool, rather than a tool that always says
        # "关闭" -- the model would learn that the capability exists and go looking.
        shell_gate_factory=(lambda: command_access) if command_access is not None else None,
        speaker=announcer,
        reminders=reminders,
    )
    app.register(tools)
    app.register(computer)

    runner = _tool_action_runner(tools)
    scheduler = SchedulerService(database, lambda: config_service.config.scheduler, runner)
    app.register(scheduler)

    workflow = WorkflowService(
        database,
        lambda: config_service.config.workflow,
        runner,
        directory_provider=lambda: paths.data_dir / config_service.config.workflow.directory,
        scheduler=scheduler,
    )
    app.register(workflow)

    plugins = PluginService(
        lambda: config_service.config.plugins,
        lambda: tools.registry,
        directory_provider=lambda: paths.data_dir / config_service.config.plugins.directory,
    )
    app.register(plugins)

    app.register(McpService(lambda: config_service.config.mcp, lambda: tools.registry))

    # The planner reads tool names but never imports the registry: ``planner``
    # sits at L3 and the dependency table does not let it reach ``tools``.
    planner = PlannerService(
        lambda: config_service.config.planner,
        llm_provider=lambda: llm_service.client,
        tool_names_provider=lambda: tools.registry.names(),
    )
    app.register(planner)

    # Vision needs OCR (it reads text off the screen) and the model (to summarise
    # what it read). Both are providers, not instances: a machine without the
    # vision extra still starts, and the failure lands on the call that needed it.
    ocr = OcrService(lambda: config_service.config.ocr)
    app.register(ocr)
    app.register(
        VisionService(
            lambda: config_service.config.vision,
            ocr_provider=lambda: ocr,
            llm_provider=lambda: llm_service.client,
        )
    )
    app.register(BrowserService(lambda: config_service.config.browser))
    # ``computer`` was registered above, next to the tool service that exposes it.

    return _Capabilities(
        database=database,
        announcer=announcer,
        reminders=reminders,
        usage=usage,
        vector=vector,
        memory=memory,
        knowledge=knowledge,
        tools=tools,
        scheduler=scheduler,
        workflow=workflow,
        plugins=plugins,
        prompts=prompts,
        planner=planner,
    )


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

    def speak_now(self) -> bool:
        pipeline = self._orchestration.pipeline
        return pipeline is not None and pipeline.speak_now()

    def speak_text(self, text: str) -> bool:
        """Read one already-shown sentence aloud; ``False`` if it cannot."""
        pipeline = self._orchestration.pipeline
        return pipeline is not None and pipeline.utter(text)

    def stop_speaking(self) -> bool:
        pipeline = self._orchestration.pipeline
        return pipeline is not None and pipeline.cancel_utterance()


def _voice_stack_builder(
    config_service: ConfigService,
    llm_service: LlmService,
    player_factory: Callable[[], AudioPlayer] | None = None,
    voice_provider: Callable[[], str | None] | None = None,
    transcript_sink: Callable[[str, str], None] | None = None,
) -> LoopBuilder:
    """Return the callable that loads the voice models when the page asks.

    The config sections are read at call time and forced enabled. Their ``enabled``
    flags gate model loading inside the services, and a desktop user pressing a
    button has not edited ``asr.enabled`` -- the consent was the button, resolved
    here by the composition root rather than by a silent default.

    ``player_factory`` overrides where synthesized audio goes. The desktop passes a
    :class:`~jarvis.orchestration.player.BridgePlayer` bound to the window, so the
    page hears the same samples the speaker would and can draw the rhythm off
    them; a console run leaves it out and gets PortAudio.
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
            player_factory=player_factory,
            on_event=report,
            voice_provider=voice_provider,
            transcript_sink=transcript_sink,
        )
        return _VoiceStack([asr, tts], orchestration)

    return build


PROBE_UTTER_TIMEOUT_SECONDS: float = 120.0
"""How long ``--speak`` waits for the voice stack. Generous: the first launch of the
day downloads about 900 MB of weights."""


def _schedule_probe_utterance(voice: VoiceService, text: str) -> None:
    """Read one sentence aloud as soon as the voice stack is up.

    Exists because the spoken-answer chain has three parts that can each fail
    silently -- synthesis, the audio bridge, and the page's playback -- and until
    now the only way to exercise all three in the real window was to have a working
    model key, a working microphone, and someone to speak into it. This needs none
    of those, and the log line it leaves behind says which of the three it reached.
    """

    def wait_and_speak() -> None:
        deadline = time.monotonic() + PROBE_UTTER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if voice.status.phase is VoicePhase.RUNNING:
                break
            time.sleep(0.5)
        else:
            logger.warning(
                "--speak gave up: the voice stack never came up (%s)", voice.status.detail
            )
            return
        if not voice.speak_text(text):
            logger.warning("--speak refused: the assistant is busy or the turn is open")
            return
        logger.info("--speak handed %d characters to the voice stack", len(text))

    threading.Thread(target=wait_and_speak, name="jarvis-speak-probe", daemon=True).start()


def _run_desktop(args: argparse.Namespace) -> int:
    """Start the services and hand them to the desktop HUD window."""
    app = Application()
    config_service = ConfigService(config_file=args.config)
    app.register(config_service)
    # See _bootstrap_cli: the capability stack needs a resolved data directory,
    # and start() is idempotent so the later full walk is a no-op.
    config_service.start()
    app.register(LoggingService(_logging_settings_factory(config_service, verbose=args.verbose)))
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
    # One Preferences object for the whole desktop: the settings overrides and the
    # remembered microphone consent are the same file, and two readers of it would
    # each hold a stale snapshot of the other's writes.
    preferences = Preferences(config_service.paths.preferences_file)
    settings = SettingsService(
        preferences,
        llm_service,
        lambda: config_service.config.llm,
    )
    app.register(settings)
    # Memory, knowledge, tools, scheduler, workflow, plugins, MCP, vision,
    # browser and desktop control. Each self-gates on its own ``enabled`` flag,
    # so a disabled capability reports itself as disabled rather than being
    # silently absent from the window.
    computer_access = ComputerAccess(preferences, lambda: config_service.config.computer)
    app.register(computer_access)
    # A separate axis from 桌面控制 on purpose: "it may type keys" and "it may run a
    # command" are different risks, and the second one defaults to off.
    command_access = CommandAccess(
        preferences,
        config_service.paths.audit_dir / "commands.jsonl",
    )
    app.register(command_access)
    # Ending a process is as irreversible as deleting a file, so it gets the same
    # treatment: an audit file of its own, and a door the page has to knock on twice.
    process_service = ProcessService(
        lambda: ProcessController(
            audit_log=config_service.paths.audit_dir / "process-kills.jsonl",
        )
    )
    capabilities = _register_capabilities(
        app,
        config_service,
        llm_service,
        computer_access,
        command_access,
    )
    # Conversations outlive the window: the store is the same SQLite file the token
    # ledger and memory already use, so "where does my history live" has one answer.
    transcript = TranscriptService(capabilities.database)
    app.register(transcript)
    chat = ChatService(
        lambda: llm_service.client,
        memory_provider=lambda: capabilities.memory,
        knowledge_provider=lambda: capabilities.knowledge,
        tool_provider=lambda: capabilities.tools,
        transcript=transcript,
    )
    app.register(chat)
    # Deliberately *not* registered here: WakeWordService and VadService each run
    # their own capture loop, and a second one would fight the voice pipeline for
    # the same microphone. Mic exclusivity is enforced by what this list contains,
    # not by a config value somebody could set twice.
    #
    # Built before the voice service because the voice stack is wired to it when it
    # boots -- which happens when the page asks, tens of seconds after this line --
    # and it needs a window before it can push anything, which the desktop shell
    # hands it once the window exists.
    from jarvis.ui.audio_bridge import AudioPusher

    audio_pusher = AudioPusher()
    # The picker is built here rather than inside the voice stack because it has
    # two customers with different lifetimes: the pipeline reads the chosen voice
    # per utterance, and the HUD's popup needs the list and the preview channel
    # whether or not the microphone was ever enabled.
    voice_picker = VoicePicker(
        lambda: config_service.config.tts,
        preferences,
        emit=audio_pusher.emit,
    )
    app.register(voice_picker)
    voice = VoiceService(
        _voice_stack_builder(
            config_service,
            llm_service,
            player_factory=lambda: BridgePlayer(audio_pusher.emit),
            voice_provider=voice_picker.effective_voice,
            transcript_sink=chat.append_turn,
        ),
        permission=lambda: args.voice or config_service.config.orchestration.enabled,
        keywords=lambda: config_service.config.wakeword.keywords,
        preferences=preferences,
    )
    app.register(voice)

    try:
        app.start()
    except Exception:
        logger.exception("desktop mode failed to start")
        return 1
    try:
        from jarvis.ui import desktop

        # Out loud is the announcer's other half, and it can only be attached now:
        # the voice service exists from the start, but whether it will ever load a
        # synthesiser is the operator's decision. With neither channel bound an
        # announcement says so instead of reporting a delivery that never happened.
        capabilities.announcer.bind(speak=voice.speak_text)
        if args.speak:
            _schedule_probe_utterance(voice, args.speak)
        # -v doubles as "show me the page's console": WebView2 devtools are the
        # only way to see what the bridge actually answered.
        return desktop.run(
            system,
            disk,
            voice=voice,
            chat=chat,
            usage=capabilities.usage,
            settings=settings,
            audio=audio_pusher,
            tools=capabilities.tools,
            voice_picker=voice_picker,
            computer_access=computer_access,
            command_access=command_access,
            process=process_service,
            preferences=preferences,
            tray_enabled=not args.no_tray,
            reminders=capabilities.reminders,
            announcer=capabilities.announcer,
            memory=capabilities.memory,
            knowledge=capabilities.knowledge,
            instance_lock=config_service.paths.data_dir / "desktop.lock",
            executable=Path(sys.executable) if getattr(sys, "frozen", False) else None,
            debug=args.verbose,
        )
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


@dataclass(frozen=True, slots=True)
class _CliContext:
    """A started application plus the services the CLI commands reach for."""

    app: Application
    config_service: ConfigService
    llm_service: LlmService
    capabilities: _Capabilities

    def close(self) -> None:
        self.app.stop()


def _bootstrap_cli(args: argparse.Namespace) -> _CliContext:
    """Start config, logging, the model and the capability stack.

    Raises:
        JarvisError: if configuration cannot be loaded. The caller turns this
            into a one-line message rather than a traceback, because the usual
            cause is a typo in a YAML file the user is looking at right now.
    """
    app = Application()
    config_service = ConfigService(config_file=args.config)
    app.register(config_service)
    # Started here, not left to Application.start(): the capability stack below
    # needs the resolved data directory to construct the database, and a path is
    # only knowable once the data root has been decided. start() is idempotent,
    # so the normal start-up walk calling it again is a no-op.
    config_service.start()
    app.register(LoggingService(_logging_settings_factory(config_service, verbose=args.verbose)))
    llm_service = LlmService(lambda: config_service.config.llm)
    app.register(llm_service)
    capabilities = _register_capabilities(app, config_service, llm_service)
    app.start()
    return _CliContext(
        app=app,
        config_service=config_service,
        llm_service=llm_service,
        capabilities=capabilities,
    )


def _run_ingest(args: argparse.Namespace) -> int:
    """Add files to the knowledge base and report what happened."""
    context = _bootstrap_cli(args)
    try:
        knowledge = context.capabilities.knowledge
        target = args.ingest
        if not target.exists():
            print(f"路径不存在：{target}", file=sys.stderr)
            return 1
        results = (
            knowledge.ingest_directory(target, force=args.force)
            if target.is_dir()
            else [knowledge.ingest(target, force=args.force)]
        )
        added = replaced = skipped = failed = 0
        for result in results:
            if result.error:
                failed += 1
                print(f"  失败  {result.source}：{result.error}", file=sys.stderr)
            elif result.skipped_reason:
                skipped += 1
                print(f"  跳过  {result.source}：{result.skipped_reason}")
            else:
                replaced += 1 if result.replaced else 0
                added += 0 if result.replaced else 1
                verb = "更新" if result.replaced else "新增"
                print(f"  {verb}  {result.source}（{result.chunks} 个片段）")
        stats = knowledge.stats()
        print(
            f"\n完成：新增 {added}，更新 {replaced}，跳过 {skipped}，失败 {failed}。"
            f"知识库现有 {stats['documents']} 个文档、{stats['chunks']} 个片段。"
        )
        return 0 if failed == 0 else 1
    finally:
        context.close()


def _run_ask(args: argparse.Namespace) -> int:
    """Answer one question from the command line, with the full stack."""
    context = _bootstrap_cli(args)
    try:
        chat = ChatService(
            lambda: context.llm_service.client,
            memory_provider=lambda: context.capabilities.memory,
            knowledge_provider=lambda: context.capabilities.knowledge,
            tool_provider=lambda: context.capabilities.tools,
            session_id="cli",
        )
        chat.start()
        reply = chat.ask(args.ask)
        if reply.error:
            print(f"回答失败：{reply.error}", file=sys.stderr)
            return 1
        print(reply.answer)
        if reply.tools_used:
            print(f"\n（调用了工具：{'、'.join(reply.tools_used)}）", file=sys.stderr)
        if reply.sources:
            print("（依据资料：" + "、".join(reply.sources) + "）", file=sys.stderr)
        return 0
    finally:
        context.close()


def _run_tools(args: argparse.Namespace) -> int:
    """List every registered tool with its risk level and permissions."""
    context = _bootstrap_cli(args)
    try:
        tools = context.capabilities.tools
        specs = tools.specs()
        if not specs:
            print("没有注册任何工具（检查 tools.enabled）")
            return 0
        print(f"共 {len(specs)} 个工具：\n")
        for spec in specs:
            permissions = f"  [{', '.join(sorted(spec.permissions))}]" if spec.permissions else ""
            print(f"  {spec.name:<18} {spec.risk.value:<10}{permissions}")
            print(f"  {'':<18} {spec.description}")
        policy = tools.stats()["policy"]
        assert isinstance(policy, Mapping)
        print(
            f"\n策略：写入={'允许' if policy['allow_write'] else '禁止'}"
            f"，执行命令={'允许' if policy['allow_shell'] else '禁止'}"
            f"，危险操作需确认={'是' if policy['confirm_dangerous'] else '否'}"
        )
        print(f"可访问目录：{'、'.join(str(root) for root in policy['file_roots'])}")
        return 0
    finally:
        context.close()


def _run_memory(args: argparse.Namespace) -> int:
    """Show, or erase, everything the assistant has remembered."""
    context = _bootstrap_cli(args)
    try:
        memory = context.capabilities.memory
        if args.forget:
            answer = input("这会删除全部长期记忆，且不可恢复。输入 yes 确认：").strip()
            if answer != "yes":
                print("已取消。")
                return 0
            removed = memory.forget_scope(MemoryScope.USER)
            print(f"已删除 {removed} 条记忆。")
            return 0

        records = memory.list_memories(limit=200)
        stats = memory.stats()
        if not records:
            print("还没有记住任何东西。")
        else:
            print(f"共 {len(records)} 条记忆：\n")
            for record in records:
                pinned = "📌 " if record.pinned else ""
                print(f"  {pinned}[{record.kind.value}] {record.content}")
                print(f"      重要度 {record.importance:.2f}  被使用 {record.access_count} 次")
        by_kind = stats["by_kind"]
        assert isinstance(by_kind, Mapping)
        summary = "、".join(f"{kind} {count}" for kind, count in by_kind.items()) or "无"
        print(f"\n分类统计：{summary}")
        sessions = stats["sessions"]
        assert isinstance(sessions, list)
        if sessions:
            print(f"最近 {len(sessions)} 个会话已记录对话轮次。")
        return 0
    finally:
        context.close()


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


def _run_prompts(args: argparse.Namespace) -> int:
    """List the prompt templates, their versions and their placeholders."""
    context = _bootstrap_cli(args)
    try:
        prompts = context.capabilities.prompts
        templates = prompts.templates()
        print(f"共 {len(templates)} 条提示词：\n")
        for template in templates:
            variables = "、".join(template.variables) if template.variables else "（无变量）"
            print(f"  {template.name:<22} v{template.version}  {len(template.body)} 字")
            print(f"  {'':<22} 变量：{variables}")
            print(f"  {'':<22} {template.description.splitlines()[0]}")
        overridden = prompts.overridden
        if overridden:
            print(f"\n以下条目已被配置覆盖：{'、'.join(overridden)}")
        else:
            print("\n全部使用内置措辞。要替换某一条，在用户配置里写 prompt.overrides。")
        return 0
    finally:
        context.close()


def _run_plan(args: argparse.Namespace) -> int:
    """Break a goal into steps and print the plan."""
    context = _bootstrap_cli(args)
    try:
        plan = context.capabilities.planner.plan(args.plan)
        print(f"目标：{plan.goal}")
        print(f"状态：{plan.status.value}（第 {plan.revision} 版）\n")
        for step in plan.steps:
            action = f"  → {step.action}" if step.action else ""
            depends = f"  （依赖 {'、'.join(step.depends_on)}）" if step.depends_on else ""
            print(f"  {step.step_id}  {step.title}{action}{depends}")
        ready = [step.step_id for step in plan.ready()]
        print(f"\n可以立即开始的步骤：{'、'.join(ready) if ready else '无'}")
        return 0
    finally:
        context.close()


def _run_cli(command: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """Run a one-shot command, turning startup failures into one line.

    A traceback is the wrong shape here: the usual cause is a typo in a YAML
    file the user is looking at, and a stack trace buries that under twenty
    frames of import machinery.
    """
    try:
        return command(args)
    except JarvisError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except Exception:
        logger.exception("command failed")
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run the application and return a process exit code."""
    args = build_arg_parser().parse_args(argv)
    _configure_bootstrap_logging(verbose=args.verbose)

    if args.wav is not None:
        return _run_demo(args)
    if args.desktop:
        return _run_desktop(args)
    # The one-shot command-line modes share a bootstrap; each prints a result and
    # exits, so they never reach the microphone loop below.
    if args.ingest is not None:
        return _run_cli(_run_ingest, args)
    if args.ask is not None:
        return _run_cli(_run_ask, args)
    if args.tools:
        return _run_cli(_run_tools, args)
    if args.memory:
        return _run_cli(_run_memory, args)
    if args.prompts:
        return _run_cli(_run_prompts, args)
    if args.plan is not None:
        return _run_cli(_run_plan, args)

    app = Application()
    config_service = ConfigService(config_file=args.config)
    app.register(config_service)

    app.register(LoggingService(_logging_settings_factory(config_service, verbose=args.verbose)))
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
