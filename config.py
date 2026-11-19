"""config.py — settings schema + persistence for dm.py.

campaigns/settings.json (override with DM_SETTINGS) is the single source of truth. Edit it by hand
or on the web UI's /config page. Values apply live, except fields marked "restart".
On the very first run, DM_* env vars / a legacy .env are imported once; after that .env is ignored.
"""
import json, os, pathlib, re, threading

BASE = pathlib.Path(__file__).parent
SETTINGS_PATH = pathlib.Path(os.environ.get("DM_SETTINGS") or BASE / "campaigns" / "settings.json")

DEFAULT_SYSTEM = """You are the Dungeon Master of a text RPG.
Rules:
- Narrate vividly but concisely (120-220 words per turn). End with the situation awaiting the player's action.
- Never act or decide for the player. Never mention dice or mechanics.
- PREMISE, STORY SO FAR and the CANON LEDGER are canon and override your own recollection. Names, counts, locations, debts, wounds, promises and items persist until the story changes them.
- The play is shown verbatim below. Before inventing a detail, check whether it was already established. Numbers, ownership and locations must not drift.
- Messages starting with "note to dm" or "ooc" are out-of-character corrections: apply them silently and do not narrate them.
- Speak NPC dialogue directly. Stay consistent with the established world.
- Vary your openings and imagery; never echo phrasing from earlier turns."""


def F(key, label, type, default, group, help, scope="global", **kw):
    return {"key": key, "label": label, "type": type, "default": default, "group": group, "help": help, "scope": scope, **kw}


FIELDS = [
    # ---- connection
    F("base_url", "Endpoint", "str", "https://openrouter.ai/api/v1", "Connection", "OpenAI-compatible base URL.", scope="global"),
    F("api_key", "API key", "secret", "", "Connection", "Stored in settings.json (chmod 600). Never sent back to the browser.", scope="global"),
    F("model", "Narrator model", "str", "", "Connection", "e.g. baidu/ernie-5.5-flash-preview:free", scope="global"),
    F("fallback_model", "Fallback model", "str", "", "Connection", "Tried once when the main model still fails after all retries. Empty = none.", scope="global"),
    F("scribe_model", "Scribe model", "str", "", "Connection", "Ledger / chapters / audit. Empty = same as narrator.", scope="global"),
    F("scribe_temp", "Scribe temperature", "optfloat", "0", "Connection", "Empty = omit (for models that reject it).", scope="global"),
    F("narrator_temp", "Narrator temperature", "optfloat", "", "Connection", "Empty = provider default.", scope="global"),
    F("narrator_max_tokens", "Narrator max output tokens", "int", 0, "Connection", "0 = omit. Raise it for reasoning models that spend output on thinking.", scope="global", min=0),
    F("analyst_model", "Analyst model", "str", "", "Connection", "Empty = scribe_model, then model.", scope="global"),
    F("planner_model", "Planner model", "str", "", "Connection", "Empty = scribe_model, then model.", scope="global"),
    # ---- context
    F("ctx_budget_tok", "Fold above (tokens)", "int", 350000, "Context & memory",
      "When the verbatim history passes this, the oldest part is folded into a chapter. Keep well under the model's window.", scope="global", min=2000),
    F("ctx_target_tok", "Fold down to (tokens)", "int", 200000, "Context & memory",
      "Verbatim history size after a fold. Big gap = rare folds = stable cacheable prefix.", scope="global", min=1000),
    F("chars_per_tok", "Characters per token", "float", 4.0, "Context & memory",
      "Estimator calibration. Compare 'context ≈' with the provider figure and adjust.", scope="global", min=1.0, max=10.0),
    F("keep_min_turns", "Always-verbatim exchanges", "int", 8, "Context & memory", "A fold never touches the latest N exchanges.", scope="global", min=1),
    F("summary_ratio", "Chapter compression ratio", "int", 25, "Context & memory", "Chapter length ≈ folded words / ratio (250–2500 words).", scope="global", min=2),
    F("ledger_enabled", "Canon ledger", "bool", True, "Context & memory", "Keyed canon state appended as the last message of every prompt.", scope="global"),
    F("ledger_lag_turns", "Ledger lag (exchanges)", "int", 6, "Context & memory", "The ledger trails the latest N exchanges so /undo and /retry stay safe.", scope="global", min=1),
    F("ledger_every_turns", "Ledger update every (exchanges)", "int", 6, "Context & memory", "", scope="global", min=1),
    F("ledger_max", "Ledger max entries", "int", 500, "Context & memory", "Over the cap, the oldest closed entries go first.", scope="global", min=20),
    F("ledger_closed_max", "Closed entries shown", "int", 80, "Context & memory", "How many resolved entries the narrator sees.", scope="global", min=0),
    F("scribe_chunk_tok", "Scribe chunk (tokens)", "int", 60000, "Context & memory", "Max play text per scribe call. Lower it for small-context scribe models.", scope="global", min=1000),
    F("audit_max_tok", "Audit window (tokens)", "int", 500000, "Context & memory", "Newest history the continuity audit reads.", scope="global", min=2000),
    F("audit_every_turns", "Auto audit every (exchanges)", "int", 0, "Context & memory", "0 = manual only (button or --audit).", scope="global", min=0),
    # ---- gameplay
    F("difficulty", "Difficulty", "select", "standard", "Gameplay", "Game difficulty (story/standard/hard).",
      scope="campaign", options=["story", "standard", "hard"]),
    F("arc_enabled", "Hidden arc", "bool", False, "Gameplay", "Enable hidden arc, clock, and pacing manager.", scope="campaign"),
    # ---- style
    F("style_enabled", "Writing style enforcement", "bool", False, "Style", "Enable writing style enforcement.", scope="campaign"),
    F("style_preset", "Style preset", "select", "none", "Style", "Preset style to apply.",
      scope="campaign", options=["none", "subtle", "pulp", "noir", "terse"]),
    F("style_words_min", "Min words per reply", "int", 0, "Style", "Minimum words per reply (0 = off).", scope="campaign", min=0),
    F("style_words_max", "Max words per reply", "int", 0, "Style", "Maximum words per reply (0 = off).", scope="campaign", min=0),
    F("style_push", "Style push (0–100)", "int", 60, "Style", "Style adherence push level.", scope="campaign", min=0, max=100),
    F("style_notes", "Style notes", "text", "", "Style", "Custom notes or instructions for writing style.", scope="campaign"),
    F("style_guard", "Style guard", "bool", False, "Style", "Prevent repetitive openers and overused phrases.", scope="campaign"),
    # ---- analyst & notes
    F("analyst_enabled", "Analyst brief", "bool", False, "Analyst", "Produce director's brief before narrator.", scope="campaign"),
    F("notes_routing", "Notes routing", "bool", False, "Notes", "Route OOC notes outside of history into notes.jsonl.", scope="campaign"),
    # ---- reliability
    F("retries", "Retries", "int", 6, "Reliability", "On 429/5xx/timeouts/empty replies.", scope="global", min=0, max=20),
    F("retry_base_sec", "Retry base delay (s)", "float", 5.0, "Reliability", "Exponential; Retry-After is honored. Capped at 90 s.", scope="global", min=0.5),
    F("timeout_sec", "Read timeout (s)", "int", 600, "Reliability", "Long prompts need long timeouts.", scope="global", min=10),
    F("analyst_timeout_sec", "Analyst timeout (s)", "int", 25, "Reliability", "Timeout for analyst call in seconds.", scope="global", min=1),
    F("analyst_context_turns", "Analyst context exchanges", "int", 10, "Reliability", "Exchanges sent to analyst as context.", scope="global", min=1),
    F("record_wait_sec", "Record wait (s)", "int", 3, "Reliability", "Max seconds analyst waits for record pass.", scope="global", min=0),
    # ---- prompt
    F("system_prompt", "DM system prompt", "text", DEFAULT_SYSTEM, "Prompt", "Persona and rules for the narrator. Applies from the next turn.", scope="global", required=True),
    # ---- app
    F("host", "Web host", "str", "0.0.0.0", "App", "Use 0.0.0.0 in Docker, and put auth in front of it.", scope="global", restart=True),
    F("port", "Web port", "int", 7860, "App", "", scope="global", restart=True, min=1, max=65535),
    F("campaign", "Default campaign", "str", "default", "App", "Folder under campaigns/. Switching needs a restart.", scope="global", restart=True),
    F("web_password", "Web password", "secret", "", "App", "Optional password for HTTP Basic auth. Empty = no auth.", scope="global"),
]

ENV_MAP = {"DM_BASE_URL": "base_url", "DM_API_KEY": "api_key", "DM_MODEL": "model", "SCRIBE_MODEL": "scribe_model",
           "SCRIBE_TEMP": "scribe_temp", "HISTORY_TURNS": "keep_min_turns", "DM_PORT": "port", "CAMPAIGN": "campaign"}


class Config:
    def __init__(self, path):
        self._path = path
        self._campaign_path = None
        self._campaign_vals = {}
        self._lock = threading.RLock()
        self._by = {f["key"]: f for f in FIELDS}
        self._vals = {f["key"]: f["default"] for f in FIELDS}
        self._load()

    # attribute access: cfg.model, cfg.ctx_budget_tok ...
    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            f = self._by[key]
            if f.get("scope") == "campaign" and key in self._campaign_vals:
                return self._campaign_vals[key]
            return self._vals[key]
        except KeyError:
            raise AttributeError(key)

    def set_campaign_path(self, path):
        with self._lock:
            self._campaign_path = pathlib.Path(path) if path else None
            self._campaign_vals = {}
            if self._campaign_path and self._campaign_path.exists():
                try:
                    data = json.loads(self._campaign_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for k, v in data.items():
                            f = self._by.get(k)
                            if f and f.get("scope") == "campaign":
                                try:
                                    self._campaign_vals[k] = self._coerce(f, v)
                                except ValueError:
                                    pass
                except (ValueError, OSError) as e:
                    print(f"[config] {self._campaign_path} unreadable ({e}); ignoring campaign overrides")

    @staticmethod
    def _coerce(f, v):
        t = f["type"]
        if t == "bool":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        if t == "select":
            s = "" if v is None else str(v).strip()
            opts = f.get("options") or []
            if opts and s not in opts:
                raise ValueError(f"must be one of: {', '.join(opts)}")
            return s
        if t in ("int", "float"):
            try:
                n = float(v) if t == "float" else int(float(v))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("must be a number")
            if "min" in f and n < f["min"]:
                raise ValueError(f"must be ≥ {f['min']}")
            if "max" in f and n > f["max"]:
                raise ValueError(f"must be ≤ {f['max']}")
            return n
        s = "" if v is None else str(v)
        if t == "optfloat":
            s = s.strip()
            if s:
                try:
                    float(s)
                except ValueError:
                    raise ValueError("must be a number or empty")
            return s
        s = s.replace("\r\n", "\n").strip()
        if f["key"] == "campaign":
            s = re.sub(r"[^\w.\-]", "_", s) or "default"
        if f.get("required") and not s:
            raise ValueError("cannot be empty")
        return s

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                print(f"[config] {self._path} unreadable ({e}); using defaults, file left untouched")
                return
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in self._by:
                        try:
                            self._vals[k] = self._coerce(self._by[k], v)
                        except ValueError:
                            print(f"[config] ignoring invalid value for {k}")
            return
        n = self._seed_from_env()
        self.save()
        print(f"[config] created {self._path}" + (f" (imported {n} values from env/.env)" if n else ""))

    def _seed_from_env(self):
        src = {}
        envfile = BASE / ".env"
        if envfile.exists():
            for line in envfile.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    src[k.strip()] = v.strip().strip('"').strip("'")
        src.update({k: os.environ[k] for k in ENV_MAP if k in os.environ})
        n = 0
        for env_key, key in ENV_MAP.items():
            if env_key in src and src[env_key] != "":
                try:
                    self._vals[key] = self._coerce(self._by[key], src[env_key])
                    n += 1
                except ValueError:
                    pass
        return n

    def save(self):
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._vals, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self._path)

    def save_campaign(self):
        with self._lock:
            if not self._campaign_path:
                return
            self._campaign_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._campaign_path.with_name(self._campaign_path.name + ".tmp")
            tmp.write_text(json.dumps(self._campaign_vals, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._campaign_path)

    def update(self, changes, scope="global"):
        """All-or-nothing. Returns {} on success, else {key: error}."""
        errors, new = {}, {}
        for k, v in (changes or {}).items():
            f = self._by.get(k)
            if not f:
                errors[k] = "unknown setting"
                continue
            if scope == "campaign" and f.get("scope") != "campaign":
                errors[k] = "not a campaign-scoped setting"
                continue
            try:
                new[k] = self._coerce(f, v)
            except ValueError as e:
                errors[k] = str(e)
        with self._lock:
            if scope == "campaign":
                if errors:
                    return errors
                self._campaign_vals.update(new)
                self.save_campaign()
                return {}
            merged = {**self._vals, **new}
            if merged["ctx_target_tok"] >= merged["ctx_budget_tok"]:
                errors["ctx_target_tok"] = "must be smaller than 'Fold above'"
            if errors:
                return errors
            self._vals = merged
            self.save()
        return {}

    def reset(self, scope="global"):
        with self._lock:
            if scope == "campaign":
                self._campaign_vals = {}
                if self._campaign_path and self._campaign_path.exists():
                    self._campaign_path.write_text("{}", encoding="utf-8")
                return
            key = self._vals["api_key"]
            self._vals = {f["key"]: f["default"] for f in FIELDS}
            self._vals["api_key"] = key          # keep the secret
            self.save()

    def public(self):
        with self._lock:
            resolved = {}
            for k, f in self._by.items():
                if f["type"] == "secret":
                    resolved[k] = ""
                elif f.get("scope") == "campaign" and k in self._campaign_vals:
                    resolved[k] = self._campaign_vals[k]
                else:
                    resolved[k] = self._vals.get(k, f["default"])
            return {
                "fields": FIELDS,
                "values": resolved,
                "global_values": {k: ("" if self._by[k]["type"] == "secret" else v) for k, v in self._vals.items()},
                "campaign_values": {k: ("" if self._by[k]["type"] == "secret" else v) for k, v in self._campaign_vals.items()},
                "secret_set": {k: bool(self._vals.get(k) or self._campaign_vals.get(k)) for k, f in self._by.items() if f["type"] == "secret"},
                "path": str(self._path),
                "campaign_path": str(self._campaign_path) if self._campaign_path else None,
            }


cfg = Config(SETTINGS_PATH)
