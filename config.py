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


def F(key, label, type, default, group, help, **kw):
    return {"key": key, "label": label, "type": type, "default": default, "group": group, "help": help, **kw}


FIELDS = [
    # ---- connection
    F("base_url", "Endpoint", "str", "https://openrouter.ai/api/v1", "Connection", "OpenAI-compatible base URL."),
    F("api_key", "API key", "secret", "", "Connection", "Stored in settings.json (chmod 600). Never sent back to the browser."),
    F("model", "Narrator model", "str", "", "Connection", "e.g. baidu/ernie-5.5-flash-preview:free"),
    F("fallback_model", "Fallback model", "str", "", "Connection", "Tried once when the main model still fails after all retries. Empty = none."),
    F("scribe_model", "Scribe model", "str", "", "Connection", "Ledger / chapters / audit. Empty = same as narrator."),
    F("scribe_temp", "Scribe temperature", "optfloat", "0", "Connection", "Empty = omit (for models that reject it)."),
    F("narrator_temp", "Narrator temperature", "optfloat", "", "Connection", "Empty = provider default."),
    F("narrator_max_tokens", "Narrator max output tokens", "int", 0, "Connection", "0 = omit. Raise it for reasoning models that spend output on thinking.", min=0),
    # ---- context
    F("ctx_budget_tok", "Fold above (tokens)", "int", 350000, "Context & memory",
      "When the verbatim history passes this, the oldest part is folded into a chapter. Keep well under the model's window.", min=2000),
    F("ctx_target_tok", "Fold down to (tokens)", "int", 200000, "Context & memory",
      "Verbatim history size after a fold. Big gap = rare folds = stable cacheable prefix.", min=1000),
    F("chars_per_tok", "Characters per token", "float", 4.0, "Context & memory",
      "Estimator calibration. Compare 'context ≈' with the provider figure and adjust.", min=1.0, max=10.0),
    F("keep_min_turns", "Always-verbatim exchanges", "int", 8, "Context & memory", "A fold never touches the latest N exchanges.", min=1),
    F("summary_ratio", "Chapter compression ratio", "int", 25, "Context & memory", "Chapter length ≈ folded words / ratio (250–2500 words).", min=2),
    F("ledger_enabled", "Canon ledger", "bool", True, "Context & memory", "Keyed canon state appended as the last message of every prompt."),
    F("ledger_lag_turns", "Ledger lag (exchanges)", "int", 6, "Context & memory", "The ledger trails the latest N exchanges so /undo and /retry stay safe.", min=1),
    F("ledger_every_turns", "Ledger update every (exchanges)", "int", 6, "Context & memory", "", min=1),
    F("ledger_max", "Ledger max entries", "int", 500, "Context & memory", "Over the cap, the oldest closed entries go first.", min=20),
    F("ledger_closed_max", "Closed entries shown", "int", 80, "Context & memory", "How many resolved entries the narrator sees.", min=0),
    F("scribe_chunk_tok", "Scribe chunk (tokens)", "int", 60000, "Context & memory", "Max play text per scribe call. Lower it for small-context scribe models.", min=1000),
    F("audit_max_tok", "Audit window (tokens)", "int", 500000, "Context & memory", "Newest history the continuity audit reads.", min=2000),
    F("audit_every_turns", "Auto audit every (exchanges)", "int", 0, "Context & memory", "0 = manual only (button or --audit).", min=0),
    # ---- reliability
    F("retries", "Retries", "int", 6, "Reliability", "On 429/5xx/timeouts/empty replies.", min=0, max=20),
    F("retry_base_sec", "Retry base delay (s)", "float", 5.0, "Reliability", "Exponential; Retry-After is honored. Capped at 90 s.", min=0.5),
    F("timeout_sec", "Read timeout (s)", "int", 600, "Reliability", "Long prompts need long timeouts.", min=10),
    # ---- prompt
    F("system_prompt", "DM system prompt", "text", DEFAULT_SYSTEM, "Prompt", "Persona and rules for the narrator. Applies from the next turn.", required=True),
    # ---- app
    F("host", "Web host", "str", "0.0.0.0", "App", "Use 0.0.0.0 in Docker, and put auth in front of it.", restart=True),
    F("port", "Web port", "int", 7860, "App", "", restart=True, min=1, max=65535),
    F("campaign", "Default campaign", "str", "default", "App", "Folder under campaigns/. Switching needs a restart.", restart=True),
]

ENV_MAP = {"DM_BASE_URL": "base_url", "DM_API_KEY": "api_key", "DM_MODEL": "model", "SCRIBE_MODEL": "scribe_model",
           "SCRIBE_TEMP": "scribe_temp", "HISTORY_TURNS": "keep_min_turns", "DM_PORT": "port", "CAMPAIGN": "campaign"}


class Config:
    def __init__(self, path):
        self._path = path
        self._lock = threading.RLock()
        self._by = {f["key"]: f for f in FIELDS}
        self._vals = {f["key"]: f["default"] for f in FIELDS}
        self._load()

    # attribute access: cfg.model, cfg.ctx_budget_tok ...
    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._vals[key]
        except KeyError:
            raise AttributeError(key)

    @staticmethod
    def _coerce(f, v):
        t = f["type"]
        if t == "bool":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
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

    def update(self, changes):
        """All-or-nothing. Returns {} on success, else {key: error}."""
        errors, new = {}, {}
        for k, v in (changes or {}).items():
            f = self._by.get(k)
            if not f:
                errors[k] = "unknown setting"
                continue
            try:
                new[k] = self._coerce(f, v)
            except ValueError as e:
                errors[k] = str(e)
        with self._lock:
            merged = {**self._vals, **new}
            if merged["ctx_target_tok"] >= merged["ctx_budget_tok"]:
                errors["ctx_target_tok"] = "must be smaller than 'Fold above'"
            if errors:
                return errors
            self._vals = merged
            self.save()
        return {}

    def reset(self):
        with self._lock:
            key = self._vals["api_key"]
            self._vals = {f["key"]: f["default"] for f in FIELDS}
            self._vals["api_key"] = key          # keep the secret
            self.save()

    def public(self):
        with self._lock:
            return {
                "fields": FIELDS,
                "values": {k: ("" if self._by[k]["type"] == "secret" else v) for k, v in self._vals.items()},
                "secret_set": {k: bool(self._vals[k]) for k, f in self._by.items() if f["type"] == "secret"},
                "path": str(self._path),
            }


cfg = Config(SETTINGS_PATH)
