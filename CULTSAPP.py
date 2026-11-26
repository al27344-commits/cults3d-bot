from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Cults3D Studio Pro & Bot",
    page_icon="🧊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==============================================================================
# ESTILO VISUAL PROFISSIONAL (CSS)
# ==============================================================================
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    div[data-testid="stMetric"] {
        background-color: rgba(128, 128, 128, 0.06);
        border: 1px solid rgba(128, 128, 128, 0.18);
        border-radius: 10px;
        padding: 12px 16px;
    }
    div[data-testid="stMetric"] label {
        font-size: 0.82rem !important;
        opacity: 0.85;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 8px 16px;
        font-weight: 600;
    }
    .seo-card {
        background-color: rgba(128, 128, 128, 0.07);
        border-left: 4px solid #00a8e8;
        border-radius: 8px;
        padding: 14px 18px;
        margin-bottom: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==============================================================================
# UTILITÁRIOS GRAPHQL & PERSISTÊNCIA EM DISCO
# ==============================================================================
class CultsAPIError(Exception):
    pass


def unwrap_gql_type(type_obj: Optional[Dict[str, Any]]) -> str:
    if not type_obj:
        return "String"
    kind = type_obj.get("kind")
    name = type_obj.get("name")
    of_type = type_obj.get("ofType")
    if kind == "NON_NULL":
        return f"{unwrap_gql_type(of_type)}!"
    if kind == "LIST":
        return f"[{unwrap_gql_type(of_type)}]"
    return name or "String"


def get_state_file_path() -> str:
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if os.access(base_dir, os.W_OK):
            return os.path.join(base_dir, "cults3d_studio_state.json")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "cults3d_studio_state.json")


STATE_FILE = get_state_file_path()


# ==============================================================================
# GOVERNADOR DE QUOTA API (PROTEÇÃO CONTRA BLOQUEIOS 429)
# ==============================================================================
class ApiRateGovernor:
    def __init__(self):
        self._lock = threading.Lock()
        self.last_call_ts = 0.0
        self.min_interval = 0.55
        self.day_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.calls_today = 0

    def wait_and_count(self):
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self.day_str:
                self.day_str = today
                self.calls_today = 0

            now = time.monotonic()
            elapsed = now - self.last_call_ts
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

            self.last_call_ts = time.monotonic()
            self.calls_today += 1

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self.day_str:
                self.day_str = today
                self.calls_today = 0
            return {
                "day": self.day_str,
                "calls_today": self.calls_today,
                "daily_soft_limit": 500,
            }


@st.cache_resource
def get_rate_governor() -> ApiRateGovernor:
    return ApiRateGovernor()


GOVERNOR = get_rate_governor()


# ==============================================================================
# CLIENTE GRAPHQL CULTS3D (AUTO-ADAPTÁVEL)
# ==============================================================================
class Cults3DClient:
    ENDPOINT = "https://cults3d.com/graphql"

    FALLBACK_MUTATIONS: Dict[str, Dict[str, str]] = {
        "createDiscount": {
            "creationId": "ID!",
            "discountPercentage": "Int!",
            "discountEndAt": "ISO8601DateTime!",
        },
        "updateCreation": {
            "id": "ID!",
            "name": "String",
            "description": "String",
            "downloadPrice": "Float",
            "price": "Float",
            "currency": "CurrencyEnum",
            "tags": "[String!]",
        },
        "createCreation": {
            "name": "String!",
            "description": "String!",
            "categoryId": "ID!",
            "fileUrls": "[String!]!",
            "imageUrls": "[String!]!",
            "downloadPrice": "Float",
            "price": "Float",
            "currency": "CurrencyEnum",
            "tags": "[String!]",
            "locale": "LocaleEnum",
        },
    }

    def __init__(self, username: str, api_key: str):
        self.session = requests.Session()
        self.session.auth = (username.strip(), api_key.strip())
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._schema_cache: Optional[Dict[str, Any]] = None

    def execute(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        for attempt in range(retries):
            GOVERNOR.wait_and_count()
            try:
                response = self.session.post(self.ENDPOINT, json=payload, timeout=30)
            except requests.RequestException as exc:
                if attempt == retries - 1:
                    raise CultsAPIError(f"Erro de ligação ao Cults3D: {exc}")
                time.sleep(2)
                continue

            if response.status_code == 401:
                raise CultsAPIError("Autenticação falhou (401). Confirma o teu username e API Key.")

            if response.status_code == 429:
                if attempt < retries - 1:
                    time.sleep(20)
                    continue
                raise CultsAPIError("Limite de pedidos atingido (429). Aguarda 30 segundos.")

            if response.status_code >= 500:
                if attempt < retries - 1:
                    time.sleep(3)
                    continue
                raise CultsAPIError(f"Erro interno do servidor Cults3D ({response.status_code}).")

            response.raise_for_status()
            data = response.json()

            if "errors" in data and data["errors"]:
                msgs = "; ".join(err.get("message", str(err)) for err in data["errors"])
                raise CultsAPIError(f"Erro GraphQL: {msgs}")

            return data.get("data") or {}

        return {}

    def get_schema_info(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self._schema_cache is not None and not force_refresh:
            return self._schema_cache

        type_fragment = """
        name
        kind
        ofType {
          name
          kind
          ofType {
            name
            kind
            ofType {
              name
              kind
              ofType {
                name
                kind
              }
            }
          }
        }
        """

        query = f"""
        query {{
          __schema {{
            queryType {{
              name
              fields {{
                name
                args {{
                  name
                  type {{ {type_fragment} }}
                }}
              }}
            }}
            mutationType {{
              name
              fields {{
                name
                args {{
                  name
                  type {{ {type_fragment} }}
                }}
              }}
            }}
          }}
          creationType: __type(name: "Creation") {{
            fields {{
              name
            }}
          }}
          categoryType: __type(name: "Category") {{
            fields {{
              name
            }}
          }}
        }}
        """
        raw = self.execute(query)
        schema_root = raw.get("__schema") or {}

        def parse_fields(type_node: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
            result: Dict[str, Dict[str, str]] = {}
            for field in (type_node or {}).get("fields") or []:
                args_map = {
                    arg["name"]: unwrap_gql_type(arg.get("type"))
                    for arg in (field.get("args") or [])
                }
                result[field["name"]] = args_map
            return result

        parsed_queries = parse_fields(schema_root.get("queryType"))
        parsed_mutations = parse_fields(schema_root.get("mutationType"))

        for m_name, m_args in self.FALLBACK_MUTATIONS.items():
            if m_name not in parsed_mutations:
                parsed_mutations[m_name] = m_args

        self._schema_cache = {
            "query_type_name": (schema_root.get("queryType") or {}).get("name", "QueryType"),
            "mutation_type_name": (schema_root.get("mutationType") or {}).get("name", "MutationType"),
            "queries": parsed_queries,
            "mutations": parsed_mutations,
            "creation_fields": {
                f["name"] for f in ((raw.get("creationType") or {}).get("fields") or [])
            },
            "category_fields": {
                f["name"] for f in ((raw.get("categoryType") or {}).get("fields") or [])
            },
        }
        return self._schema_cache

    def test_connection(self) -> Dict[str, Any]:
        query = """
        query {
          myself {
            user {
              nick
              url
            }
          }
        }
        """
        myself_data = self.execute(query).get("myself") or {}
        return myself_data.get("user") or {}

    def get_categories(self) -> List[Dict[str, str]]:
        schema = self.get_schema_info()
        if "categories" not in schema["queries"]:
            return []

        cat_fields = schema["category_fields"]
        sub_field = None
        for candidate in ("subcategories", "children", "subCategories"):
            if candidate in cat_fields:
                sub_field = candidate
                break

        if sub_field:
            query = f"""
            query {{
              categories {{
                id
                name
                {sub_field} {{
                  id
                  name
                }}
              }}
            }}
            """
        else:
            query = """
            query {
              categories {
                id
                name
              }
            }
            """

        cats = self.execute(query).get("categories") or []
        flat_list: List[Dict[str, str]] = []
        for c in cats:
            c_id = str(c.get("id", ""))
            c_name = str(c.get("name", c_id))
            if c_id:
                flat_list.append({"id": c_id, "label": c_name})
            if sub_field:
                for sub in c.get(sub_field) or []:
                    s_id = str(sub.get("id", ""))
                    s_name = str(sub.get("name", s_id))
                    if s_id:
                        flat_list.append({"id": s_id, "label": f"{c_name} > {s_name}"})
        return flat_list

    def get_all_my_creations(self, currency: str = "EUR") -> List[Dict[str, Any]]:
        schema = self.get_schema_info()
        c_fields = schema["creation_fields"]
        desc_field = "description" if "description" in c_fields else ""

        query = f"""
        query($limit: Int!, $offset: Int!, $currency: CurrencyEnum!) {{
          myself {{
            creationsBatch(limit: $limit, offset: $offset) {{
              total
              results {{
                id
                name
                slug
                url
                {desc_field}
                viewsCount
                likesCount
                downloadsCount
                publishedAt
                tags
                price(currency: $currency) {{
                  cents
                  currency
                  formatted
                }}
              }}
            }}
          }}
        }}
        """
        all_items: List[Dict[str, Any]] = []
        offset = 0
        limit = 50

        while True:
            data = self.execute(query, {"limit": limit, "offset": offset, "currency": currency})
            batch = (data.get("myself") or {}).get("creationsBatch") or {}
            results = batch.get("results") or []
            total = batch.get("total") or 0
            all_items.extend(results)
            offset += limit
            if offset >= total or not results:
                break

        return all_items

    def get_all_my_sales(self, currency: str = "EUR") -> List[Dict[str, Any]]:
        query = """
        query($limit: Int!, $offset: Int!, $currency: CurrencyEnum!) {
          myself {
            salesBatch(limit: $limit, offset: $offset) {
              total
              results {
                id
                createdAt
                income(currency: $currency) {
                  cents
                  currency
                  formatted
                }
                creation {
                  id
                  name
                  slug
                }
                user {
                  nick
                }
              }
            }
          }
        }
        """
        all_sales: List[Dict[str, Any]] = []
        offset = 0
        limit = 50

        while True:
            data = self.execute(query, {"limit": limit, "offset": offset, "currency": currency})
            batch = (data.get("myself") or {}).get("salesBatch") or {}
            results = batch.get("results") or []
            total = batch.get("total") or 0
            all_sales.extend(results)
            offset += limit
            if offset >= total or not results:
                break

        return all_sales

    def search_market(
        self,
        search_query: str,
        limit: int = 40,
        only_priced: bool = False,
        currency: str = "EUR",
    ) -> Dict[str, Any]:
        """Pesquisa rigorosa no mercado com paginação e prioridade para creationsSearchBatch."""
        schema = self.get_schema_info()
        queries = schema["queries"]

        # Priorizar sempre creationsSearchBatch quando há termo de pesquisa
        search_field = "creationsSearchBatch"
        if "creationsSearchBatch" not in queries and "creationsBatch" in queries:
            search_field = "creationsBatch"

        allowed_args = queries.get(search_field) or {
            "query": "String!",
            "limit": "Int",
            "offset": "Int",
            "onlyPriced": "Boolean",
        }

        collected: List[Dict[str, Any]] = []
        total_found = 0
        offset = 0
        target_limit = max(1, int(limit))

        while len(collected) < target_limit:
            batch_size = min(50, target_limit - len(collected))
            candidate_values: Dict[str, Any] = {
                "query": search_query.strip(),
                "limit": batch_size,
                "offset": offset,
            }
            if only_priced and "onlyPriced" in allowed_args:
                candidate_values["onlyPriced"] = True

            var_defs = ["$currency: CurrencyEnum!"]
            call_args = []
            variables: Dict[str, Any] = {"currency": currency}

            for arg_name, arg_val in candidate_values.items():
                if arg_name in allowed_args:
                    gql_type = allowed_args[arg_name]
                    var_defs.append(f"${arg_name}: {gql_type}")
                    call_args.append(f"{arg_name}: ${arg_name}")
                    variables[arg_name] = arg_val

            var_header = ", ".join(var_defs)
            args_header = f"({', '.join(call_args)})" if call_args else ""

            query = f"""
            query({var_header}) {{
              {search_field}{args_header} {{
                total
                results {{
                  id
                  name
                  url
                  viewsCount
                  likesCount
                  downloadsCount
                  publishedAt
                  tags
                  creator {{
                    nick
                  }}
                  price(currency: $currency) {{
                    cents
                    formatted
                  }}
                }}
              }}
            }}
            """
            data = self.execute(query, variables)
            batch = data.get(search_field) or {}
            results = batch.get("results") or []
            total_found = int(batch.get("total") or len(results))

            if not results:
                break

            collected.extend(results)
            offset += len(results)
            if "offset" not in allowed_args or offset >= total_found:
                break

        return {"total": total_found, "results": collected[:target_limit]}

    def _build_dynamic_mutation(
        self,
        mutation_name: str,
        raw_params: Dict[str, Any],
        return_selection: str,
    ) -> Dict[str, Any]:
        schema = self.get_schema_info()
        mutations = schema["mutations"]
        if mutation_name not in mutations:
            disponiveis = ", ".join(sorted(mutations.keys())) or "nenhuma"
            raise CultsAPIError(
                f"A mutation '{mutation_name}' não existe no schema ({disponiveis})."
            )

        allowed_args = mutations[mutation_name]
        var_defs: List[str] = []
        call_args: List[str] = []
        variables: Dict[str, Any] = {}

        for arg_name, arg_val in raw_params.items():
            if arg_name not in allowed_args:
                continue
            gql_type = allowed_args[arg_name]
            if arg_val is None and not gql_type.endswith("!"):
                continue

            if gql_type.startswith("Int") and isinstance(arg_val, float):
                arg_val = int(round(arg_val))

            var_defs.append(f"${arg_name}: {gql_type}")
            call_args.append(f"{arg_name}: ${arg_name}")
            variables[arg_name] = arg_val

        var_header = f"({', '.join(var_defs)})" if var_defs else ""
        args_header = f"({', '.join(call_args)})" if call_args else ""

        mutation = f"""
        mutation{var_header} {{
          {mutation_name}{args_header} {{
            {return_selection}
          }}
        }}
        """
        res = self.execute(mutation, variables).get(mutation_name) or {}
        if res.get("errors"):
            raise CultsAPIError(f"Erro Cults3D: {res['errors']}")
        return res

    def create_creation(
        self,
        name: str,
        description: str,
        category_id: str,
        file_urls: List[str],
        image_urls: List[str],
        price: float = 0.0,
        currency: str = "EUR",
        tags: Optional[List[str]] = None,
        locale: str = "EN",
    ) -> Dict[str, Any]:
        params = {
            "name": name,
            "description": description,
            "categoryId": category_id,
            "fileUrls": file_urls,
            "imageUrls": image_urls,
            "price": float(price),
            "downloadPrice": float(price),
            "currency": currency,
            "tags": tags or [],
            "locale": locale,
        }
        res = self._build_dynamic_mutation(
            "createCreation",
            params,
            "creation { id name url } errors",
        )
        return res.get("creation") or {}

    def update_creation(
        self,
        creation_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        price: Optional[float] = None,
        currency: str = "EUR",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "id": creation_id,
            "creationId": creation_id,
            "name": name,
            "description": description,
            "price": float(price) if price is not None else None,
            "downloadPrice": float(price) if price is not None else None,
            "currency": currency if price is not None else None,
            "tags": tags,
        }
        res = self._build_dynamic_mutation(
            "updateCreation",
            params,
            "creation { id name url } errors",
        )
        return res.get("creation") or {}

    def create_discount(self, creation_id: str, percentage: int, end_at_iso: str) -> bool:
        params = {
            "creationId": creation_id,
            "discountPercentage": int(percentage),
            "discountEndAt": end_at_iso,
        }
        self._build_dynamic_mutation("createDiscount", params, "errors")
        return True

    def introspect_type(self, type_name: str) -> Dict[str, Any]:
        schema = self.get_schema_info()
        resolved_name = type_name
        if type_name.lower() == "mutation":
            resolved_name = schema.get("mutation_type_name", "MutationType")
        elif type_name.lower() == "query":
            resolved_name = schema.get("query_type_name", "QueryType")

        query = """
        query($name: String!) {
          __type(name: $name) {
            name
            fields {
              name
              args {
                name
                type { name kind ofType { name kind ofType { name kind } } }
              }
            }
          }
        }
        """
        return self.execute(query, {"name": resolved_name}).get("__type") or {}


# ==============================================================================
# MOTOR DO BOT AUTÓNOMO (10 MÓDULOS + COMANDOS TELEGRAM + PERSISTÊNCIA)
# ==============================================================================
class CultsBotEngine:
    DEFAULT_CONFIG: Dict[str, Any] = {
        "interval_minutes": 15,
        "dry_run": True,
        "max_actions_per_cycle": 5,
        "tg_token": "",
        "tg_chat_id": "",
        "tg_remote_commands": True,
        "discord_webhook": "",
        # 1. Vendas & Milestones
        "rule_sales_alert": True,
        "rule_milestone_alert": True,
        # 2. Picos de Tráfego
        "rule_spike_alert": True,
        "spike_views_threshold": 35,
        # 3. Auto-Promo (0 Vendas)
        "rule_auto_promo": False,
        "promo_min_views": 120,
        "promo_discount_pct": 20,
        "promo_days": 3,
        "promo_cooldown_days": 14,
        # 4. Reativação de Modelos Estagnados (tinham vendas, pararam)
        "rule_stagnant_revival": False,
        "stagnant_days": 30,
        "stagnant_discount_pct": 25,
        "stagnant_promo_days": 3,
        # 5. Preço Dinâmico por Procura
        "rule_dynamic_price": False,
        "price_step_sales": 10,
        "price_bump_pct": 15.0,
        "price_max_cap": 15.0,
        # 6. Promoção Relâmpago de Fim de Semana
        "rule_weekend_flash": False,
        "weekend_day": 4,  # 4 = Sexta-feira
        "weekend_hour": 18,
        "weekend_discount_pct": 20,
        "weekend_days": 3,
        # 7. Otimizador SEO & Rodapé
        "rule_seo_optimizer": False,
        "seo_footer_marker": "--- FSDesigns ---",
        "seo_footer_text": "\n\n--- FSDesigns ---\nDesigned for reliable 3D printing (PETG / ABS / ASA recommended for automotive parts). Follow our profile for new functional designs!",
        "seo_required_tags": "stl, 3d print",
        # 8. Fila de Publicação Agendada
        "rule_queue_publisher": True,
        # 9. Radar de Nichos
        "rule_market_watch": False,
        "watch_keywords": "",
        # 10. Relatório Diário & Semanal
        "rule_daily_report": True,
        "daily_report_hour": 21,
        "rule_weekly_report": True,
    }

    DEFAULT_PRESETS: Dict[str, Dict[str, Any]] = {
        "Automóvel / Motorsport (PETG / ASA)": {
            "category_hint": "gadget",
            "price": 3.50,
            "locale": "EN",
            "tags": "car, automotive, motorsport, petg, asa, custom part, tuning, functional print",
            "overview": "Custom functional automotive part engineered for OEM+ fitment and durability.",
            "print_settings": "- Material: ASA, ABS or PETG (mandatory for cabin/engine bay heat resistance)\n- Layer height: 0.20 mm\n- Infill: 30% - 40% Gyroid\n- Perimeters/Walls: 4 walls\n- Supports: Tree supports only on critical overhangs",
            "assembly": "Test-fit before final installation. Clean mounting surface with isopropyl alcohol if using automotive double-sided tape.",
            "license": "Personal use only. Contact FSDesigns for commercial printing rights.",
        },
        "Gadgets & Articulados (PLA)": {
            "category_hint": "toy",
            "price": 2.50,
            "locale": "EN",
            "tags": "articulated, flexi, print in place, fidget, toy, pla, cute, desk toy",
            "overview": "Print-in-place articulated model designed for smooth movement right off the build plate with zero assembly.",
            "print_settings": "- Material: PLA / PLA+ / Silk PLA\n- Layer height: 0.16 mm - 0.20 mm\n- Infill: 15% Gyroid\n- Supports: None required (Print-in-Place)\n- Brim: Only if bed adhesion is weak",
            "assembly": "Gently flex joints after cooling to break any minor tolerance bridges.",
            "license": "Personal use only. Do not redistribute or sell STL files.",
        },
        "Organização & Oficina (PLA / PETG)": {
            "category_hint": "tool",
            "price": 2.00,
            "locale": "EN",
            "tags": "organizer, holder, mount, workshop, tool, functional, storage, stl",
            "overview": "Heavy-duty organizer and mount engineered for clean workspace storage and fast printing.",
            "print_settings": "- Material: PETG or PLA+\n- Layer height: 0.24 mm (0.4 mm or 0.6 mm nozzle)\n- Infill: 20% Grid/Gyroid\n- Perimeters: 3 walls\n- Supports: None",
            "assembly": "Mounts using standard countersunk wood/wall screws.",
            "license": "Personal use only.",
        },
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self.running = False
        self._thread: Optional[threading.Thread] = None

        self.username = ""
        self.api_key = ""
        self.currency = "EUR"

        self.config: Dict[str, Any] = dict(self.DEFAULT_CONFIG)
        self.presets: Dict[str, Dict[str, Any]] = dict(self.DEFAULT_PRESETS)
        self.seen_sale_ids: List[str] = []
        self.sales_seeded: bool = False
        self.celebrated_milestones: List[str] = []
        self.promo_cooldowns: Dict[str, str] = {}
        self.price_tiers_applied: Dict[str, int] = {}
        self.seo_processed_ids: List[str] = []
        self.seen_market_ids: List[str] = []
        self.market_seeded: bool = False
        self.model_snapshots: Dict[str, Dict[str, Any]] = {}
        self.daily_baseline: Dict[str, Any] = {}
        self.weekly_baseline: Dict[str, Any] = {}
        self.last_daily_report_date: str = ""
        self.last_weekly_report_week: str = ""
        self.last_weekend_flash_week: str = ""
        self.tg_last_update_id: int = 0
        self.publish_queue: List[Dict[str, Any]] = []
        self.action_history: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, str]] = []
        self.last_run: Optional[str] = None

        self._load_state()

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self.config.update(data.get("config") or {})
                saved_presets = data.get("presets") or {}
                if saved_presets:
                    self.presets.update(saved_presets)
                self.seen_sale_ids = list(data.get("seen_sale_ids") or [])
                self.sales_seeded = bool(data.get("sales_seeded", False))
                self.celebrated_milestones = list(data.get("celebrated_milestones") or [])
                self.promo_cooldowns = dict(data.get("promo_cooldowns") or {})
                self.price_tiers_applied = dict(data.get("price_tiers_applied") or {})
                self.seo_processed_ids = list(data.get("seo_processed_ids") or [])
                self.seen_market_ids = list(data.get("seen_market_ids") or [])
                self.market_seeded = bool(data.get("market_seeded", False))
                self.model_snapshots = dict(data.get("model_snapshots") or {})
                self.daily_baseline = dict(data.get("daily_baseline") or {})
                self.weekly_baseline = dict(data.get("weekly_baseline") or {})
                self.last_daily_report_date = str(data.get("last_daily_report_date") or "")
                self.last_weekly_report_week = str(data.get("last_weekly_report_week") or "")
                self.last_weekend_flash_week = str(data.get("last_weekend_flash_week") or "")
                self.tg_last_update_id = int(data.get("tg_last_update_id") or 0)
                self.publish_queue = list(data.get("publish_queue") or [])
                self.action_history = list(data.get("action_history") or [])
                self.logs = list(data.get("logs") or [])[:250]
                self.last_run = data.get("last_run")
        except Exception:
            pass

    def _save_state(self):
        with self._lock:
            payload = {
                "config": self.config,
                "presets": self.presets,
                "seen_sale_ids": self.seen_sale_ids[-2500:],
                "sales_seeded": self.sales_seeded,
                "celebrated_milestones": self.celebrated_milestones[-1000:],
                "promo_cooldowns": self.promo_cooldowns,
                "price_tiers_applied": self.price_tiers_applied,
                "seo_processed_ids": self.seo_processed_ids[-2500:],
                "seen_market_ids": self.seen_market_ids[-3000:],
                "market_seeded": self.market_seeded,
                "model_snapshots": self.model_snapshots,
                "daily_baseline": self.daily_baseline,
                "weekly_baseline": self.weekly_baseline,
                "last_daily_report_date": self.last_daily_report_date,
                "last_weekly_report_week": self.last_weekly_report_week,
                "last_weekend_flash_week": self.last_weekend_flash_week,
                "tg_last_update_id": self.tg_last_update_id,
                "publish_queue": self.publish_queue,
                "action_history": self.action_history[:250],
                "logs": self.logs[:250],
                "last_run": self.last_run,
            }
        try:
            tmp_file = f"{STATE_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, STATE_FILE)
        except Exception:
            pass

    def log(self, level: str, message: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.logs.insert(0, {"hora": ts, "tipo": level, "mensagem": message})
            self.logs = self.logs[:250]
        self._save_state()

    def seed_baselines_if_needed(self, creations: List[Dict[str, Any]]):
        if not creations:
            return
        now_local = datetime.now()
        local_date_str = now_local.strftime("%Y-%m-%d")
        iso_year, iso_week, _ = now_local.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"

        total_v = sum(int(c.get("viewsCount") or 0) for c in creations)
        total_d = sum(int(c.get("downloadsCount") or 0) for c in creations)

        changed = False
        with self._lock:
            if self.daily_baseline.get("date") != local_date_str:
                self.daily_baseline = {
                    "date": local_date_str,
                    "since_time": now_local.strftime("%H:%M"),
                    "views": total_v,
                    "downloads": total_d,
                }
                changed = True
            if self.weekly_baseline.get("week") != week_key:
                self.weekly_baseline = {
                    "week": week_key,
                    "since_date": local_date_str,
                    "views": total_v,
                    "downloads": total_d,
                }
                changed = True
        if changed:
            self._save_state()

    def record_action(
        self,
        action_type: str,
        creation_id: str,
        creation_name: str,
        summary: str,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        reversible: bool = False,
    ):
        entry = {
            "action_id": f"{int(time.time() * 1000)}_{creation_id}",
            "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tipo": action_type,
            "creation_id": str(creation_id),
            "modelo": creation_name,
            "detalhe": summary,
            "before": before_state or {},
            "after": after_state or {},
            "reversible": reversible,
            "reverted": False,
        }
        with self._lock:
            self.action_history.insert(0, entry)
            self.action_history = self.action_history[:250]
        self._save_state()

    def revert_action(self, action_id: str) -> str:
        with self._lock:
            target = next((a for a in self.action_history if a["action_id"] == action_id), None)
            user, key, curr = self.username, self.api_key, self.currency

        if not target or not target.get("reversible") or target.get("reverted"):
            raise CultsAPIError("Esta ação não pode ser revertida.")
        if not user or not key:
            raise CultsAPIError("Credenciais em falta.")

        before = target.get("before") or {}
        client = Cults3DClient(user, key)
        client.update_creation(
            creation_id=target["creation_id"],
            name=before.get("name"),
            description=before.get("description"),
            price=before.get("price"),
            currency=curr,
            tags=before.get("tags"),
        )

        with self._lock:
            target["reverted"] = True
        self.log("ROLLBACK", f"Alteração revertida em '{target['modelo']}'.")
        return f"Alteração revertida com sucesso em '{target['modelo']}'."

    def set_credentials(self, username: str, api_key: str, currency: str):
        with self._lock:
            self.username = username
            self.api_key = api_key
            self.currency = currency

    def send_notification(self, text: str):
        with self._lock:
            tg_token = str(self.config.get("tg_token", "")).strip()
            tg_chat = str(self.config.get("tg_chat_id", "")).strip()
            discord_url = str(self.config.get("discord_webhook", "")).strip()

        if tg_token and tg_chat:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": text, "parse_mode": "HTML"},
                    timeout=10,
                )
            except Exception as exc:
                self.log("ERRO", f"Falha Telegram: {exc}")

        if discord_url:
            try:
                clean_text = text.replace("<b>", "**").replace("</b>", "**")
                requests.post(discord_url, json={"content": clean_text}, timeout=10)
            except Exception as exc:
                self.log("ERRO", f"Falha Discord: {exc}")

    def poll_telegram_commands(self):
        """Escuta comandos enviados pelo dono no Telegram (/status, /vendas, /top, /ciclo, /pausar, /retomar)."""
        with self._lock:
            if not self.config.get("tg_remote_commands", True):
                return
            tg_token = str(self.config.get("tg_token", "")).strip()
            allowed_chat = str(self.config.get("tg_chat_id", "")).strip()
            offset = self.tg_last_update_id + 1 if self.tg_last_update_id > 0 else None
            user, key, curr = self.username, self.api_key, self.currency

        if not tg_token or not allowed_chat:
            return

        try:
            params: Dict[str, Any] = {"timeout": 1, "limit": 10}
            if offset:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{tg_token}/getUpdates",
                params=params,
                timeout=5,
            )
            if resp.status_code != 200:
                return
            updates = resp.json().get("result") or []
            if not updates:
                return

            max_id = self.tg_last_update_id
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                if uid > max_id:
                    max_id = uid
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id") or "")
                text = str(msg.get("text") or "").strip().lower()

                if chat_id != allowed_chat or not text.startswith("/"):
                    continue

                cmd = text.split()[0]
                if cmd == "/status":
                    gov = GOVERNOR.get_stats()
                    state_lbl = "🟢 ATIVO" if self.running else "🔴 PAUSADO"
                    mode_lbl = "🧪 Simulação" if self.config.get("dry_run") else "⚡ Real"
                    self.send_notification(
                        f"🤖 <b>Estado Cults3D Bot</b>\n"
                        f"Estado: {state_lbl} ({mode_lbl})\n"
                        f"Último ciclo: {self.last_run or 'Nunca'}\n"
                        f"Quota API hoje: {gov['calls_today']}/{gov['daily_soft_limit']}"
                    )
                elif cmd == "/pausar":
                    self.stop()
                    self.send_notification("⏸️ Bot pausado remotamente via Telegram.")
                elif cmd == "/retomar":
                    self.start()
                    self.send_notification("▶️ Bot retomado remotamente via Telegram.")
                elif cmd == "/ciclo":
                    self.send_notification("⚡ A executar ciclo manual agora...")
                    threading.Thread(target=self.run_cycle, daemon=True).start()
                elif cmd in ("/vendas", "/top") and user and key:
                    c_api = Cults3DClient(user, key)
                    if cmd == "/vendas":
                        sales = c_api.get_all_my_sales(currency=curr)[:5]
                        if not sales:
                            self.send_notification("Ainda não existem vendas registadas.")
                        else:
                            lines = ["💰 <b>Últimas 5 Vendas:</b>"]
                            for s in sales:
                                m = (s.get("creation") or {}).get("name") or "?"
                                val = ((s.get("income") or {}).get("cents") or 0) / 100.0
                                b = (s.get("user") or {}).get("nick") or "Anónimo"
                                lines.append(f"• {m}: <b>+{val:.2f} {curr}</b> ({b})")
                            self.send_notification("\n".join(lines))
                    elif cmd == "/top":
                        creations = c_api.get_all_my_creations(currency=curr)
                        top5 = sorted(creations, key=lambda x: int(x.get("viewsCount") or 0), reverse=True)[:5]
                        lines = ["🏆 <b>Top 5 Modelos (Views):</b>"]
                        for idx, item in enumerate(top5, 1):
                            lines.append(
                                f"{idx}. {item.get('name')} — {item.get('viewsCount')} views | {item.get('downloadsCount')} dl"
                            )
                        self.send_notification("\n".join(lines))

            with self._lock:
                self.tg_last_update_id = max_id
            self._save_state()
        except Exception:
            pass

    def start(self):
        with self._lock:
            if self.running:
                return
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        self.log(
            "INFO",
            f"Bot contínuo iniciado (intervalo: {self.config['interval_minutes']} min | Dry-Run: {self.config['dry_run']}).",
        )

    def stop(self):
        with self._lock:
            self.running = False
        self.log("INFO", "Bot contínuo parado.")

    def _loop(self):
        while True:
            with self._lock:
                if not self.running:
                    break
                wait_seconds = max(60, int(self.config.get("interval_minutes", 15)) * 60)

            self.run_cycle()

            for step in range(wait_seconds):
                with self._lock:
                    if not self.running:
                        return
                if step % 6 == 0:
                    self.poll_telegram_commands()
                time.sleep(1)

    def _is_in_promo_cooldown(self, creation_id: str) -> bool:
        until_str = self.promo_cooldowns.get(str(creation_id))
        if not until_str:
            return False
        try:
            until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) < until_dt
        except Exception:
            return False

    def run_cycle(self):
        if not self._cycle_lock.acquire(blocking=False):
            return

        try:
            with self._lock:
                user = self.username
                key = self.api_key
                curr = self.currency
                cfg = dict(self.config)

            if not user or not key:
                self.log("AVISO", "Credenciais em falta no bot.")
                return

            client = Cults3DClient(user, key)
            dry = bool(cfg.get("dry_run", True))
            max_actions = int(cfg.get("max_actions_per_cycle", 5))
            actions_done = 0
            mode_tag = "[SIMULAÇÃO]" if dry else "[REAL]"
            self.log("CICLO", f"A iniciar ciclo autónomo {mode_tag}...")

            # ------------------------------------------------------------------
            # 1. VENDAS & MARCOS (MILESTONES)
            # ------------------------------------------------------------------
            sales = client.get_all_my_sales(currency=curr)
            sales_count_map: Dict[str, int] = {}
            last_sale_date_map: Dict[str, str] = {}
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_sales_count = 0
            today_revenue = 0.0

            for s in sales:
                cr_id = str((s.get("creation") or {}).get("id") or "")
                created_at = str(s.get("createdAt") or "")
                if cr_id:
                    sales_count_map[cr_id] = sales_count_map.get(cr_id, 0) + 1
                    if created_at > last_sale_date_map.get(cr_id, ""):
                        last_sale_date_map[cr_id] = created_at
                if created_at.startswith(today_str):
                    today_sales_count += 1
                    today_revenue += ((s.get("income") or {}).get("cents") or 0) / 100.0

            if cfg.get("rule_sales_alert", True):
                seen_set = set(self.seen_sale_ids)
                if not self.sales_seeded:
                    for s in sales:
                        if s.get("id"):
                            seen_set.add(str(s["id"]))
                    with self._lock:
                        self.seen_sale_ids = list(seen_set)
                        self.sales_seeded = True
                    self.log("VENDAS", f"Memória inicializada com {len(seen_set)} vendas registadas.")
                else:
                    new_sales = [s for s in sales if s.get("id") and str(s["id"]) not in seen_set]
                    for s in reversed(new_sales):
                        sid = str(s["id"])
                        seen_set.add(sid)
                        cr_obj = s.get("creation") or {}
                        cr_id = str(cr_obj.get("id") or "")
                        m_name = cr_obj.get("name") or "Modelo"
                        inc_val = ((s.get("income") or {}).get("cents") or 0) / 100.0
                        buyer = (s.get("user") or {}).get("nick") or "Anónimo"
                        total_model_sales = sales_count_map.get(cr_id, 1)

                        msg = (
                            f"💰 <b>Nova Venda Cults3D!</b>\n"
                            f"📦 <b>Modelo:</b> {m_name}\n"
                            f"💵 <b>Receita Líquida:</b> +{inc_val:.2f} {curr}\n"
                            f"👤 <b>Comprador:</b> {buyer}\n"
                            f"🏆 <b>Total deste modelo:</b> {total_model_sales} vendas"
                        )
                        self.log("VENDA", f"Nova venda: '{m_name}' (+{inc_val:.2f} {curr}) por {buyer}.")
                        self.send_notification(msg)

                        # Celebração de Marcos
                        if cfg.get("rule_milestone_alert") and total_model_sales in (5, 10, 25, 50, 100, 250):
                            m_key = f"{cr_id}_sales_{total_model_sales}"
                            if m_key not in self.celebrated_milestones:
                                with self._lock:
                                    self.celebrated_milestones.append(m_key)
                                self.send_notification(
                                    f"🎉 <b>Marco Atingido!</b>\nO modelo <b>{m_name}</b> chegou às <b>{total_model_sales} vendas</b>!"
                                )

                    with self._lock:
                        self.seen_sale_ids = list(seen_set)

            # ------------------------------------------------------------------
            # 2. CATÁLOGO: TRÁFEGO, AUTO-PROMO, REATIVAÇÃO, PREÇO, FIM DE SEMANA, SEO
            # ------------------------------------------------------------------
            creations = client.get_all_my_creations(currency=curr)
            self.seed_baselines_if_needed(creations)
            spike_alerts: List[str] = []

            # Verificar se é hora de Promoção Relâmpago de Fim de Semana
            now_local = datetime.now()
            iso_y, iso_w, _ = now_local.isocalendar()
            current_week_key = f"{iso_y}-W{iso_w:02d}"
            trigger_weekend_flash = (
                cfg.get("rule_weekend_flash")
                and now_local.weekday() == int(cfg.get("weekend_day", 4))
                and now_local.hour >= int(cfg.get("weekend_hour", 18))
                and self.last_weekend_flash_week != current_week_key
            )

            for c in creations:
                cid = str(c.get("id"))
                cname = c.get("name") or cid
                cdesc = c.get("description") or ""
                ctags = list(c.get("tags") or [])
                cviews = int(c.get("viewsCount") or 0)
                cdowns = int(c.get("downloadsCount") or 0)
                cprice = round(((c.get("price") or {}).get("cents") or 0) / 100.0, 2)
                csales = sales_count_map.get(cid, 0)

                # --- Pico de Tráfego ---
                prev_snap = self.model_snapshots.get(cid)
                if cfg.get("rule_spike_alert") and prev_snap:
                    delta_views = cviews - int(prev_snap.get("views", cviews))
                    if delta_views >= int(cfg.get("spike_views_threshold", 35)):
                        spike_alerts.append(f"• <b>{cname}</b>: +{delta_views} views (total: {cviews})")

                with self._lock:
                    self.model_snapshots[cid] = {
                        "name": cname,
                        "views": cviews,
                        "downloads": cdowns,
                        "price": cprice,
                        "sales": csales,
                    }

                # --- Módulo 3: Auto-Promo Inteligente (0 Vendas) ---
                if (
                    actions_done < max_actions
                    and cfg.get("rule_auto_promo")
                    and cprice > 0
                    and csales == 0
                    and cviews >= int(cfg.get("promo_min_views", 120))
                    and not self._is_in_promo_cooldown(cid)
                ):
                    pct = int(cfg.get("promo_discount_pct", 20))
                    days = int(cfg.get("promo_days", 3))
                    cd_days = int(cfg.get("promo_cooldown_days", 14))
                    end_iso = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    cd_until = (datetime.now(timezone.utc) + timedelta(days=cd_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

                    if dry:
                        self.log("SIMULAÇÃO", f"[Auto-Promo] {pct}% por {days}d em '{cname}' ({cviews} views, 0 vendas).")
                    else:
                        try:
                            client.create_discount(cid, pct, end_iso)
                            with self._lock:
                                self.promo_cooldowns[cid] = cd_until
                            actions_done += 1
                            self.log("PROMO", f"Desconto {pct}% por {days}d em '{cname}'.")
                            self.record_action("AUTO_PROMO", cid, cname, f"Desconto {pct}% por {days}d")
                            self.send_notification(f"🔥 <b>Auto-Promo Ativada</b>\nModelo: {cname}\nDesconto: {pct}% ({days} dias)")
                        except Exception as exc:
                            with self._lock:
                                self.promo_cooldowns[cid] = (
                                    datetime.now(timezone.utc) + timedelta(days=days)
                                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                            self.log("AVISO", f"Auto-Promo ignorada em '{cname}': {exc}")

                # --- Módulo 4: Reativação de Modelos Estagnados ---
                if (
                    actions_done < max_actions
                    and cfg.get("rule_stagnant_revival")
                    and cprice > 0
                    and csales > 0
                    and not self._is_in_promo_cooldown(cid)
                ):
                    last_sale_str = last_sale_date_map.get(cid, "")
                    if last_sale_str:
                        try:
                            last_dt = datetime.fromisoformat(last_sale_str.replace("Z", "+00:00"))
                            days_since = (datetime.now(timezone.utc) - last_dt).days
                            if days_since >= int(cfg.get("stagnant_days", 30)):
                                s_pct = int(cfg.get("stagnant_discount_pct", 25))
                                s_days = int(cfg.get("stagnant_promo_days", 3))
                                end_iso = (datetime.now(timezone.utc) + timedelta(days=s_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                                cd_until = (datetime.now(timezone.utc) + timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
                                if dry:
                                    self.log(
                                        "SIMULAÇÃO",
                                        f"[Reativação] '{cname}' sem vendas há {days_since}d -> desconto {s_pct}%.",
                                    )
                                else:
                                    client.create_discount(cid, s_pct, end_iso)
                                    with self._lock:
                                        self.promo_cooldowns[cid] = cd_until
                                    actions_done += 1
                                    self.log("REATIVAÇÃO", f"Desconto {s_pct}% em '{cname}' (parado há {days_since}d).")
                                    self.record_action("REATIVAÇÃO", cid, cname, f"Desconto {s_pct}% após {days_since}d sem vendas")
                        except Exception:
                            pass

                # --- Módulo 5: Preço Dinâmico por Procura ---
                if actions_done < max_actions and cfg.get("rule_dynamic_price") and cprice > 0:
                    step_sales = max(1, int(cfg.get("price_step_sales", 10)))
                    earned_tiers = csales // step_sales
                    applied_tiers = int(self.price_tiers_applied.get(cid, 0))
                    max_cap = float(cfg.get("price_max_cap", 15.0))
                    bump_pct = float(cfg.get("price_bump_pct", 15.0))

                    if earned_tiers > applied_tiers and cprice < max_cap:
                        new_price = min(round(max_cap, 2), round(cprice * (1.0 + bump_pct / 100.0), 2))
                        if new_price > cprice:
                            if dry:
                                self.log(
                                    "SIMULAÇÃO",
                                    f"[Preço Dinâmico] '{cname}': {cprice:.2f} -> {new_price:.2f} {curr} ({csales} vendas).",
                                )
                            else:
                                try:
                                    client.update_creation(creation_id=cid, price=new_price, currency=curr)
                                    with self._lock:
                                        self.price_tiers_applied[cid] = earned_tiers
                                    actions_done += 1
                                    self.log("PREÇO", f"'{cname}': {cprice:.2f} -> {new_price:.2f} {curr}.")
                                    self.record_action(
                                        "PREÇO_DINÂMICO",
                                        cid,
                                        cname,
                                        f"{cprice:.2f} -> {new_price:.2f} {curr}",
                                        before_state={"price": cprice},
                                        after_state={"price": new_price},
                                        reversible=True,
                                    )
                                    self.send_notification(
                                        f"📈 <b>Preço Atualizado</b>\nModelo: {cname}\n{cprice:.2f} ➔ <b>{new_price:.2f} {curr}</b>"
                                    )
                                except Exception as exc:
                                    self.log("ERRO", f"Falha preço em '{cname}': {exc}")

                # --- Módulo 6: Promoção Relâmpago de Fim de Semana ---
                if trigger_weekend_flash and cprice > 0 and not self._is_in_promo_cooldown(cid):
                    w_pct = int(cfg.get("weekend_discount_pct", 20))
                    w_days = int(cfg.get("weekend_days", 3))
                    end_iso = (datetime.now(timezone.utc) + timedelta(days=w_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if dry:
                        self.log("SIMULAÇÃO", f"[Flash Fim-de-Semana] {w_pct}% em '{cname}'.")
                    else:
                        try:
                            client.create_discount(cid, w_pct, end_iso)
                            with self._lock:
                                self.promo_cooldowns[cid] = end_iso
                            self.log("FLASH", f"Promoção fim-de-semana ({w_pct}%) aplicada em '{cname}'.")
                        except Exception:
                            pass

                # --- Módulo 7: Otimizador SEO & Rodapé ---
                if actions_done < max_actions and cfg.get("rule_seo_optimizer") and cid not in self.seo_processed_ids:
                    marker = str(cfg.get("seo_footer_marker", "")).strip()
                    footer = str(cfg.get("seo_footer_text", ""))
                    req_tags = [
                        t.strip().lower()
                        for t in str(cfg.get("seo_required_tags", "")).split(",")
                        if t.strip()
                    ]

                    updated_desc = cdesc
                    desc_changed = False
                    if marker and footer and marker not in cdesc:
                        updated_desc = (cdesc.rstrip() + footer).strip()
                        desc_changed = True

                    norm_tags = list(dict.fromkeys([t.strip().lower() for t in ctags if t.strip()]))
                    for rt in req_tags:
                        if rt not in norm_tags:
                            norm_tags.append(rt)
                    tags_changed = norm_tags != ctags

                    if not desc_changed and not tags_changed:
                        with self._lock:
                            self.seo_processed_ids.append(cid)
                    else:
                        if dry:
                            self.log("SIMULAÇÃO", f"[SEO] Atualizaria '{cname}' (rodapé={desc_changed}, tags={tags_changed}).")
                        else:
                            try:
                                client.update_creation(
                                    creation_id=cid,
                                    description=updated_desc if desc_changed else None,
                                    tags=norm_tags if tags_changed else None,
                                )
                                with self._lock:
                                    self.seo_processed_ids.append(cid)
                                actions_done += 1
                                self.log("SEO", f"SEO/Rodapé aplicado em '{cname}'.")
                                self.record_action(
                                    "SEO_OTIMIZADOR",
                                    cid,
                                    cname,
                                    f"Rodapé={desc_changed}, Tags={len(norm_tags)}",
                                    before_state={"description": cdesc, "tags": ctags},
                                    after_state={"description": updated_desc, "tags": norm_tags},
                                    reversible=True,
                                )
                            except Exception as exc:
                                self.log("ERRO", f"Falha SEO em '{cname}': {exc}")

            if trigger_weekend_flash and not dry:
                with self._lock:
                    self.last_weekend_flash_week = current_week_key
                self.send_notification(
                    f"⚡ <b>Promoção Relâmpago de Fim de Semana Ativa!</b>\nDesconto de {cfg.get('weekend_discount_pct')}% aplicado aos modelos elegíveis."
                )

            if spike_alerts:
                self.log("TRÁFEGO", f"{len(spike_alerts)} modelo(s) com pico de tráfego!")
                self.send_notification("🚀 <b>Pico de Tráfego Detetado!</b>\n" + "\n".join(spike_alerts[:6]))

            # ------------------------------------------------------------------
            # 3. FILA DE PUBLICAÇÃO AGENDADA
            # ------------------------------------------------------------------
            if cfg.get("rule_queue_publisher", True) and self.publish_queue:
                now_utc = datetime.now(timezone.utc)
                with self._lock:
                    queue_copy = list(self.publish_queue)

                for item in queue_copy:
                    if item.get("status") != "Pendente":
                        continue
                    sched_str = item.get("scheduled_at") or ""
                    try:
                        sched_dt = datetime.fromisoformat(sched_str.replace("Z", "+00:00"))
                        if sched_dt.tzinfo is None:
                            sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        sched_dt = now_utc

                    if now_utc >= sched_dt:
                        if dry:
                            self.log("SIMULAÇÃO", f"[Fila] Publicaria agora o modelo '{item['name']}'.")
                        else:
                            try:
                                created = client.create_creation(
                                    name=item["name"],
                                    description=item["description"],
                                    category_id=str(item["category_id"]),
                                    file_urls=item["file_urls"],
                                    image_urls=item["image_urls"],
                                    price=float(item.get("price", 0.0)),
                                    currency=curr,
                                    tags=item.get("tags") or [],
                                    locale=item.get("locale", "EN"),
                                )
                                pub_url = created.get("url") or ""
                                with self._lock:
                                    item["status"] = "Publicado"
                                    item["published_url"] = pub_url
                                self.log("PUBLICADO", f"Modelo agendado publicado: '{item['name']}' -> {pub_url}")
                                self.record_action("PUBLICAÇÃO", str(created.get("id") or "novo"), item["name"], pub_url)
                                self.send_notification(f"🚀 <b>Modelo Publicado!</b>\n📦 {item['name']}\n🔗 {pub_url}")
                            except Exception as exc:
                                with self._lock:
                                    item["status"] = f"Erro: {exc}"
                                self.log("ERRO", f"Falha ao publicar '{item['name']}': {exc}")
                        break

            # ------------------------------------------------------------------
            # 4. RADAR DE NICHOS
            # ------------------------------------------------------------------
            keywords = [k.strip() for k in str(cfg.get("watch_keywords", "")).split(",") if k.strip()]
            if cfg.get("rule_market_watch") and keywords:
                seen_m = set(self.seen_market_ids)
                found_new_items: List[str] = []

                for kw in keywords[:4]:
                    res_mkt = client.search_market(kw, limit=20, only_priced=True, currency=curr)
                    for item in res_mkt.get("results") or []:
                        mid = str(item.get("id") or "")
                        if not mid:
                            continue
                        if not self.market_seeded:
                            seen_m.add(mid)
                        elif mid not in seen_m:
                            seen_m.add(mid)
                            m_title = item.get("name") or "Sem título"
                            m_price = ((item.get("price") or {}).get("cents") or 0) / 100.0
                            m_creator = (item.get("creator") or {}).get("nick") or "?"
                            m_url = item.get("url") or ""
                            found_new_items.append(f"• <b>{m_title}</b> ({m_price:.2f} {curr}) por {m_creator}\n  {m_url}")

                with self._lock:
                    self.seen_market_ids = list(seen_m)
                    if not self.market_seeded:
                        self.market_seeded = True
                        self.log("RADAR", f"Radar inicializado com {len(seen_m)} peças monitorizadas.")

                if found_new_items:
                    self.log("RADAR", f"{len(found_new_items)} nova(s) peça(s) no radar.")
                    self.send_notification("🕵️ <b>Novidades nos Nichos Vigiados</b>\n" + "\n".join(found_new_items[:5]))

            # ------------------------------------------------------------------
            # 5. RELATÓRIOS DIÁRIO & SEMANAL (COM BASELINE CORRETO)
            # ------------------------------------------------------------------
            local_date_str = now_local.strftime("%Y-%m-%d")
            total_v = sum(int(c.get("viewsCount") or 0) for c in creations)
            total_d = sum(int(c.get("downloadsCount") or 0) for c in creations)

            if cfg.get("rule_daily_report") and creations:
                target_hour = int(cfg.get("daily_report_hour", 21))
                since_time = self.daily_baseline.get("since_time", "00:00")
                if (
                    now_local.hour >= target_hour
                    and self.last_daily_report_date != local_date_str
                    and self.daily_baseline.get("date") == local_date_str
                ):
                    delta_v = max(0, total_v - int(self.daily_baseline.get("views", total_v)))
                    delta_d = max(0, total_d - int(self.daily_baseline.get("downloads", total_d)))
                    top_model = max(creations, key=lambda x: int(x.get("viewsCount") or 0))
                    report_msg = (
                        f"📊 <b>Relatório Diário Cults3D ({local_date_str})</b>\n"
                        f"🕒 Medição desde as <b>{since_time}</b>\n"
                        f"💰 Vendas Hoje: <b>{today_sales_count}</b> ({today_revenue:.2f} {curr})\n"
                        f"👁️ Novas Views: <b>+{delta_v}</b> (Total: {total_v:,})\n"
                        f"⬇️ Novos Downloads: <b>+{delta_d}</b> (Total: {total_d:,})\n"
                        f"⭐ Destaque: {top_model.get('name')} ({top_model.get('viewsCount')} views)"
                    )
                    self.send_notification(report_msg)
                    with self._lock:
                        self.last_daily_report_date = local_date_str
                    self.log("RELATÓRIO", f"Relatório diário enviado (medição desde {since_time}).")

            # Relatório Semanal ao Domingo a partir das 20h
            if (
                cfg.get("rule_weekly_report")
                and creations
                and now_local.weekday() == 6
                and now_local.hour >= 20
                and self.last_weekly_report_week != current_week_key
            ):
                w_delta_v = max(0, total_v - int(self.weekly_baseline.get("views", total_v)))
                w_delta_d = max(0, total_d - int(self.weekly_baseline.get("downloads", total_d)))
                self.send_notification(
                    f"📅 <b>Resumo Semanal Cults3D ({current_week_key})</b>\n"
                    f"👁️ Views na semana: <b>+{w_delta_v:,}</b>\n"
                    f"⬇️ Downloads na semana: <b>+{w_delta_d:,}</b>\n"
                    f"📦 Catálogo ativo: <b>{len(creations)}</b> modelos"
                )
                with self._lock:
                    self.last_weekly_report_week = current_week_key
                self.log("RELATÓRIO", "Resumo semanal enviado.")

            with self._lock:
                self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_state()
            self.log("CICLO", "Ciclo concluído sem erros.")

        except Exception as exc:
            self.log("ERRO", f"Erro no ciclo do bot: {exc}")
        finally:
            self._cycle_lock.release()


@st.cache_resource
def get_bot_engine() -> CultsBotEngine:
    return CultsBotEngine()


bot = get_bot_engine()


# ==============================================================================
# MOTOR DE SEO & ANÁLISE DE QUALIDADE
# ==============================================================================
def analyze_listing_seo(name: str, description: str, tags: List[str], price: float) -> Dict[str, Any]:
    score = 0
    tips: List[str] = []

    # 1. Título (25 pts)
    n_len = len(name.strip())
    if n_len >= 25:
        score += 25
    elif n_len >= 12:
        score += 15
        tips.append("Alonga o título para 25–60 caracteres com modelo/dimensões (ex: '52mm', 'Mk2', 'Print-in-Place').")
    else:
        tips.append("Título demasiado curto. Inclui palavras-chave específicas que os compradores pesquisam.")

    # 2. Descrição (35 pts)
    d_len = len(description.strip())
    if d_len >= 250:
        score += 20
    elif d_len >= 100:
        score += 12
        tips.append("Expande a descrição (ideal > 250 caracteres) detalhando vantagens e montagem.")
    else:
        tips.append("Descrição muito curta. O algoritmo do Cults3D favorece descrições completas.")

    desc_lower = description.lower()
    if any(k in desc_lower for k in ("infill", "layer", "support", "petg", "pla", "abs", "asa", "nozzle")):
        score += 15
    else:
        tips.append("Adiciona parâmetros de impressão (material recomendado, altura de camada, suportes e infill).")

    # 3. Tags (30 pts)
    clean_tags = [t.strip().lower() for t in tags if t.strip()]
    if len(clean_tags) >= 8:
        score += 30
    elif len(clean_tags) >= 5:
        score += 20
        tips.append(f"Tens {len(clean_tags)} tags. Adiciona pelo menos 8 a 12 tags relevantes para maximizar o alcance.")
    else:
        score += 5
        tips.append("Poucas tags! Usa entre 8 e 15 tags (sinónimos, aplicações, material e categoria).")

    # 4. Preço psicológico (10 pts)
    if price == 0 or (round(price * 100) % 50 == 0 or str(price).endswith(("9", "5"))):
        score += 10
    else:
        score += 6

    # Sugestão automática de tags extraídas do título
    stopwords = {"the", "and", "for", "with", "para", "com", "de", "do", "da", "em", "stl", "3d"}
    words = re.findall(r"[a-zA-Z0-9À-ÿ]{3,}", name.lower())
    suggested_tags = [w for w in dict.fromkeys(words) if w not in stopwords and w not in clean_tags][:8]

    return {
        "score": min(100, score),
        "tips": tips,
        "suggested_tags": suggested_tags,
    }


# ==============================================================================
# GESTÃO DE ESTADO DA SESSÃO
# ==============================================================================
for key, default in [
    ("username", os.getenv("CULTS_USERNAME", "")),
    ("api_key", os.getenv("CULTS_API_KEY", "")),
    ("currency", "EUR"),
    ("connected", False),
    ("profile", None),
    ("creations", []),
    ("sales", []),
    ("categories", []),
]:
    if key not in st.session_state:
        st.session_state[key] = default


def sync_account(client: Cults3DClient, currency: str):
    schema = client.get_schema_info(force_refresh=True)
    profile = client.test_connection()
    creations = client.get_all_my_creations(currency=currency)
    bot.seed_baselines_if_needed(creations)
    sales = client.get_all_my_sales(currency=currency)
    try:
        categories = client.get_categories()
    except Exception:
        categories = []

    st.session_state.schema = schema
    st.session_state.profile = profile
    st.session_state.creations = creations
    st.session_state.sales = sales
    st.session_state.categories = categories
    st.session_state.connected = True


# ==============================================================================
# ECRÃ INICIAL DE LOGIN
# ==============================================================================
if not st.session_state.connected:
    st.markdown("<br>", unsafe_allow_html=True)
    _, center_col, _ = st.columns([1, 2, 1])
    with center_col:
        st.title("🧊 Cults3D Studio Pro")
        st.write(
            "Plataforma completa de Business Intelligence, Publicação SEO, Radar de Nichos e Bot Autónomo para a tua loja Cults3D."
        )

        with st.form("login_form"):
            in_user = st.text_input(
                "Username Cults3D",
                value=st.session_state.username,
                placeholder="Ex: FSDesigns",
                help="O teu nome de utilizador público (nick) no Cults3D.",
            )
            in_key = st.text_input(
                "API Key",
                value=st.session_state.api_key,
                type="password",
                placeholder="Cola aqui a tua API Key",
                help="A chave gerada nas definições da tua conta Cults3D (secção API).",
            )
            in_curr = st.selectbox(
                "Moeda principal",
                ["EUR", "USD", "GBP"],
                index=0,
                help="Moeda usada para calcular receitas, preços e análises de mercado.",
            )
            submit_login = st.form_submit_button(
                "🔌 Ligar e Sincronizar Conta",
                type="primary",
                use_container_width=True,
                help="Autentica na API GraphQL do Cults3D e carrega todo o teu catálogo e vendas.",
            )

        if submit_login:
            if not in_user.strip() or not in_key.strip():
                st.warning("Preenche o username e a API Key.")
            else:
                with st.spinner("A ligar ao Cults3D, a validar schema e a sincronizar dados..."):
                    try:
                        temp_client = Cults3DClient(in_user, in_key)
                        sync_account(temp_client, in_curr)
                        st.session_state.username = in_user.strip()
                        st.session_state.api_key = in_key.strip()
                        st.session_state.currency = in_curr
                        bot.set_credentials(in_user.strip(), in_key.strip(), in_curr)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
    st.stop()


# ==============================================================================
# BARRA LATERAL (CONTROLOS GLOBAIS)
# ==============================================================================
client = Cults3DClient(st.session_state.username, st.session_state.api_key)
currency = st.session_state.currency
bot.set_credentials(st.session_state.username, st.session_state.api_key, currency)

with st.sidebar:
    st.title("🧊 Cults3D Studio Pro")
    nick = (st.session_state.profile or {}).get("nick", st.session_state.username)
    st.success(f"Conta: **{nick}**")
    st.caption(f"📦 {len(st.session_state.creations)} modelos | 💰 {len(st.session_state.sales)} vendas")

    gov_stats = GOVERNOR.get_stats()
    st.progress(
        min(1.0, gov_stats["calls_today"] / gov_stats["daily_soft_limit"]),
        text=f"Quota API hoje: {gov_stats['calls_today']}/{gov_stats['daily_soft_limit']}",
    )

    if bot.running:
        st.info(f"🤖 Bot: **ATIVO** ({'Simulação' if bot.config['dry_run'] else 'Real'})")
    else:
        st.caption("🤖 Bot: Parado")

    new_curr = st.selectbox(
        "Moeda",
        ["EUR", "USD", "GBP"],
        index=["EUR", "USD", "GBP"].index(currency),
        help="Altera a moeda de visualização e recarrega os valores convertidos pela API.",
    )
    if new_curr != currency:
        st.session_state.currency = new_curr
        bot.set_credentials(st.session_state.username, st.session_state.api_key, new_curr)
        with st.spinner("A atualizar moeda..."):
            try:
                sync_account(client, new_curr)
                st.rerun()
            except Exception as e:
                st.error(str(e))

    if st.button(
        "🔄 Sincronizar Tudo",
        type="primary",
        use_container_width=True,
        help="Recarrega modelos, vendas, estatísticas e categorias diretamente do Cults3D.",
    ):
        with st.spinner("A sincronizar..."):
            try:
                sync_account(client, st.session_state.currency)
                st.toast("Dados atualizados!")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    with st.popover("ℹ️ Guia Rápido & Dicas", use_container_width=True):
        st.markdown(
            "**Passar o rato no `?`**: Todos os campos e métricas têm explicação detalhada ao passar o rato.\n\n"
            "**Medição de Views Diárias**: A API do Cults3D só fornece o contador total acumulado; a app grava a base inicial assim que abres e calcula o ganho diário a partir dessa hora.\n\n"
            "**Modo Simulação**: Testa todas as regras do bot sem mexer nos teus modelos reais."
        )

    if st.button(
        "🚪 Trocar Conta / API Key",
        use_container_width=True,
        help="Termina a sessão atual e permite entrar com outra conta.",
    ):
        bot.stop()
        st.session_state.connected = False
        st.session_state.api_key = ""
        st.session_state.creations = []
        st.session_state.sales = []
        st.rerun()


# ==============================================================================
# PREPARAÇÃO DE DATAFRAMES & CLASSIFICAÇÃO BCG
# ==============================================================================
def build_dataframes():
    creations_rows = []
    for c in st.session_state.creations:
        price_obj = c.get("price") or {}
        tags_raw = c.get("tags") or []
        creations_rows.append({
            "id": str(c.get("id")),
            "nome": c.get("name") or "",
            "descricao": c.get("description") or "",
            "preco": round((price_obj.get("cents") or 0) / 100.0, 2),
            "views": int(c.get("viewsCount") or 0),
            "likes": int(c.get("likesCount") or 0),
            "downloads": int(c.get("downloadsCount") or 0),
            "publicado_em": c.get("publishedAt"),
            "tags_list": tags_raw,
            "tags": ", ".join(tags_raw),
            "url": c.get("url") or "",
        })

    sales_rows = []
    for s in st.session_state.sales:
        inc = s.get("income") or {}
        cr = s.get("creation") or {}
        buyer = s.get("user") or {}
        sales_rows.append({
            "sale_id": str(s.get("id")),
            "data": s.get("createdAt"),
            "creation_id": str(cr.get("id") or ""),
            "modelo": cr.get("name") or "Modelo removido",
            "receita_liquida": round((inc.get("cents") or 0) / 100.0, 2),
            "comprador": buyer.get("nick") or "Anónimo",
        })

    return pd.DataFrame(creations_rows), pd.DataFrame(sales_rows)


df_creations, df_sales = build_dataframes()

tab_dash, tab_bot, tab_publish, tab_bulk, tab_market, tab_console = st.tabs([
    "📊 Analytics & BI",
    "🤖 Bot Autónomo",
    "🚀 Estúdio Publicação & SEO",
    "⚡ Gestão em Massa",
    "🔍 Radar de Mercado",
    "🛠️ Consola GraphQL",
])

# ==============================================================================
# ABA 1: ANALYTICS & BUSINESS INTELLIGENCE
# ==============================================================================
with tab_dash:
    if df_creations.empty:
        st.info("Ainda não tens modelos publicados nesta conta.")
    else:
        total_receita = float(df_sales["receita_liquida"].sum()) if not df_sales.empty else 0.0
        total_vendas = len(df_sales)
        ticket_medio = (total_receita / total_vendas) if total_vendas > 0 else 0.0
        total_views = int(df_creations["views"].sum())
        total_downloads = int(df_creations["downloads"].sum())
        conv_rate = (total_vendas / total_views * 100.0) if total_views > 0 else 0.0
        rpm = (total_receita / total_views * 1000.0) if total_views > 0 else 0.0

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric(
            f"Receita Líquida ({currency})",
            f"{total_receita:,.2f}",
            help="Valor líquido total ganho após as comissões do Cults3D.",
        )
        k2.metric(
            "Vendas Totais",
            f"{total_vendas}",
            help="Número total de transações pagas concluídas na conta.",
        )
        k3.metric(
            f"Ticket Médio ({currency})",
            f"{ticket_medio:,.2f}",
            help="Receita líquida média recebida por cada venda.",
        )
        k4.metric(
            "Visualizações",
            f"{total_views:,}",
            help="Soma de visualizações de todas as peças do teu portfólio.",
        )
        k5.metric(
            "Conversão Global",
            f"{conv_rate:.2f}%",
            help="Percentagem de visualizações que resultam numa venda (Vendas / Views).",
        )
        k6.metric(
            f"RPM ({currency}/1k views)",
            f"{rpm:,.2f}",
            help="Receita líquida média gerada por cada 1.000 visualizações.",
        )

        # Cruzar vendas com modelos
        if not df_sales.empty:
            sales_by_model = (
                df_sales.groupby("creation_id")
                .agg(vendas=("sale_id", "count"), receita=("receita_liquida", "sum"))
                .reset_index()
            )
            perf_df = df_creations.merge(sales_by_model, left_on="id", right_on="creation_id", how="left")
            perf_df["vendas"] = perf_df["vendas"].fillna(0).astype(int)
            perf_df["receita"] = perf_df["receita"].fillna(0.0).round(2)
        else:
            perf_df = df_creations.copy()
            perf_df["vendas"] = 0
            perf_df["receita"] = 0.0

        perf_df["conversao_%"] = (
            perf_df["vendas"] / perf_df["views"].replace(0, pd.NA) * 100.0
        ).fillna(0.0).round(2)
        perf_df["likes_ratio_%"] = (
            perf_df["likes"] / perf_df["views"].replace(0, pd.NA) * 100.0
        ).fillna(0.0).round(1)

        # Classificação Estratégica Automática (Matriz BCG)
        med_views = max(50.0, float(perf_df["views"].median()))
        med_conv = max(0.5, float(perf_df[perf_df["vendas"] > 0]["conversao_%"].median()) if (perf_df["vendas"] > 0).any() else 1.0)

        def classify_tier(row) -> str:
            if row["preco"] == 0:
                return "🎁 Gratuito (Íman)"
            if row["views"] >= med_views and row["conversao_%"] >= med_conv and row["vendas"] > 0:
                return "🏆 Estrela (Best-Seller)"
            if row["views"] < med_views and row["vendas"] > 0:
                return "💎 Joia Escondida (Precisa Tráfego)"
            if row["views"] >= med_views and row["vendas"] == 0:
                return "👀 Muito Visto / 0 Vendas (Rever Preço)"
            return "💤 Em Crescimento"

        perf_df["estado_bcg"] = perf_df.apply(classify_tier, axis=1)

        st.divider()

        # Gráficos Temporais & Comportamento do Comprador
        if not df_sales.empty:
            dt_series = pd.to_datetime(df_sales["data"], utc=True, errors="coerce")
            df_sales["dt_utc"] = dt_series
            valid_sales = df_sales.dropna(subset=["dt_utc"]).copy()

            c_left, c_right = st.columns([2, 1])
            with c_left:
                hdr1, hdr2 = st.columns([2, 1])
                hdr1.subheader("Evolução de Receita")
                agg_mode = hdr2.selectbox(
                    "Agrupamento",
                    ["Dia", "Semana", "Mês", "Acumulado"],
                    label_visibility="collapsed",
                    help="Escolhe a escala temporal para analisar o crescimento da tua receita.",
                )
                if not valid_sales.empty:
                    if agg_mode == "Dia":
                        series_plot = valid_sales.groupby(valid_sales["dt_utc"].dt.date)["receita_liquida"].sum()
                        st.bar_chart(series_plot)
                    elif agg_mode == "Semana":
                        series_plot = valid_sales.groupby(valid_sales["dt_utc"].dt.to_period("W").astype(str))["receita_liquida"].sum()
                        st.bar_chart(series_plot)
                    elif agg_mode == "Mês":
                        series_plot = valid_sales.groupby(valid_sales["dt_utc"].dt.to_period("M").astype(str))["receita_liquida"].sum()
                        st.bar_chart(series_plot)
                    else:
                        daily_s = valid_sales.groupby(valid_sales["dt_utc"].dt.date)["receita_liquida"].sum().cumsum()
                        st.area_chart(daily_s)

            with c_right:
                st.subheader("Melhores Dias de Venda")
                if not valid_sales.empty:
                    dias_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
                    valid_sales["dia_sem"] = valid_sales["dt_utc"].dt.dayofweek.map(lambda d: dias_pt[d])
                    by_day = valid_sales.groupby("dia_sem")["sale_id"].count().reindex(dias_pt, fill_value=0)
                    st.bar_chart(by_day)

        st.divider()

        # Tabela Matriz de Performance
        st.subheader("Matriz de Performance do Catálogo")
        filter_tier = st.multiselect(
            "Filtrar por Classificação Estratégica",
            options=sorted(perf_df["estado_bcg"].unique().tolist()),
            default=[],
            help="Filtra os modelos pela categoria automática (Estrelas, Joias Escondidas, Muito Visto sem Vendas, etc.).",
        )
        view_df = perf_df[perf_df["estado_bcg"].isin(filter_tier)] if filter_tier else perf_df

        st.dataframe(
            view_df[
                [
                    "nome",
                    "estado_bcg",
                    "preco",
                    "views",
                    "likes",
                    "downloads",
                    "vendas",
                    "receita",
                    "conversao_%",
                    "url",
                ]
            ].sort_values(by=["receita", "views"], ascending=False),
            use_container_width=True,
            hide_index=True,
            column_config={
                "nome": "Modelo",
                "estado_bcg": "Diagnóstico",
                "url": st.column_config.LinkColumn("Página", display_text="Abrir"),
                "preco": st.column_config.NumberColumn(f"Preço ({currency})", format="%.2f"),
                "receita": st.column_config.NumberColumn(f"Receita ({currency})", format="%.2f"),
                "conversao_%": st.column_config.NumberColumn("Conv. (%)", format="%.2f%%"),
            },
        )

        # Inspetor & Editor Rápido Individual
        with st.expander("🔬 Inspetor & Editor Rápido de Modelo Individual"):
            model_names_map = {row["nome"]: row for _, row in perf_df.iterrows()}
            chosen_name = st.selectbox(
                "Escolhe um modelo para inspecionar e editar diretamente",
                list(model_names_map.keys()),
                help="Permite ver o diagnóstico SEO/conversão de uma peça e atualizar preço, título, descrição ou tags.",
            )
            m_row = model_names_map[chosen_name]
            seo_eval = analyze_listing_seo(m_row["nome"], m_row["descricao"], m_row["tags_list"], m_row["preco"])

            ic1, ic2, ic3, ic4 = st.columns(4)
            ic1.metric("SEO Score", f"{seo_eval['score']}/100", help="Pontuação calculada com base no título, descrição e tags.")
            ic2.metric("Likes / Views", f"{m_row['likes_ratio_%']}%", help="Taxa de interesse visual da capa e renders.")
            ic3.metric("Diagnóstico", m_row["estado_bcg"].split(" (")[0])
            ic4.metric("Receita Gerada", f"{m_row['receita']:.2f} {currency}")

            if seo_eval["tips"]:
                st.warning("💡 **Sugestões de Melhoria:** " + " | ".join(seo_eval["tips"]))

            with st.form("quick_edit_single_form"):
                qe_name = st.text_input("Título", value=m_row["nome"], help="Título público do anúncio no Cults3D.")
                qe_price = st.number_input(
                    f"Preço ({currency})",
                    min_value=0.0,
                    step=0.5,
                    value=float(m_row["preco"]),
                    help="Define 0.00 para tornar gratuito.",
                )
                qe_tags = st.text_input(
                    "Tags (vírgula)",
                    value=m_row["tags"],
                    help="Palavras-chave separadas por vírgula.",
                )
                qe_desc = st.text_area(
                    "Descrição",
                    value=m_row["descricao"],
                    height=130,
                    help="Descrição técnica e instruções de impressão.",
                )
                if st.form_submit_button("💾 Guardar Alterações neste Modelo", type="primary"):
                    try:
                        client.update_creation(
                            creation_id=m_row["id"],
                            name=qe_name.strip(),
                            description=qe_desc.strip(),
                            price=qe_price,
                            currency=currency,
                            tags=[t.strip() for t in qe_tags.split(",") if t.strip()],
                        )
                        st.session_state.creations = client.get_all_my_creations(currency=currency)
                        st.success("Modelo atualizado!")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        c_exp1, c_exp2 = st.columns(2)
        c_exp1.download_button(
            "⬇️ Exportar Catálogo Completo (CSV)",
            data=perf_df.drop(columns=["tags_list"], errors="ignore").to_csv(index=False).encode("utf-8"),
            file_name="cults3d_catalogo_bi.csv",
            mime="text/csv",
            help="Descarrega todas as métricas e diagnósticos em formato Excel/CSV.",
        )
        if not df_sales.empty:
            c_exp2.download_button(
                "⬇️ Exportar Histórico de Vendas (CSV)",
                data=df_sales.drop(columns=["dt_utc"], errors="ignore").to_csv(index=False).encode("utf-8"),
                file_name="cults3d_vendas.csv",
                mime="text/csv",
                help="Descarrega todas as vendas para contabilidade.",
            )


# ==============================================================================
# ABA 2: BOT AUTÓNOMO (10 MÓDULOS + TELEGRAM REMOTO + ROLLBACK)
# ==============================================================================
with tab_bot:
    st.subheader("🤖 Bot Autónomo Cults3D (10 Módulos + Controlo Remoto)")
    st.caption(f"Estado persistente guardado em `{STATE_FILE}` — sobrevive a reinícios.")

    b1, b2, b3, b4, b5 = st.columns([1, 1, 1, 1, 1.3])
    b1.metric("Estado", "🟢 ATIVO" if bot.running else "🔴 PARADO", help="Indica se o ciclo automático em segundo plano está ligado.")
    b2.metric("Modo", "🧪 Simulação" if bot.config["dry_run"] else "⚡ Real", help="Em Simulação o bot apenas regista logs sem alterar preços/descontos.")
    b3.metric("Fila Agendada", f"{sum(1 for x in bot.publish_queue if x.get('status') == 'Pendente')}", help="Modelos prontos para publicação automática.")
    b4.metric("Último Ciclo", (bot.last_run or "Nunca").split(" ")[-1], help="Hora da última verificação automática concluída.")

    with b5:
        if not bot.running:
            if st.button(
                "▶️ Iniciar Bot Contínuo",
                type="primary",
                use_container_width=True,
                help="Inicia o motor automático em segundo plano.",
            ):
                bot.start()
                st.rerun()
        else:
            if st.button(
                "⏹️ Parar Bot",
                use_container_width=True,
                help="Pausa a execução automática do bot.",
            ):
                bot.stop()
                st.rerun()

        if st.button(
            "⚡ Executar 1 Ciclo Agora",
            use_container_width=True,
            help="Força a execução imediata de todos os módulos ativos neste instante.",
        ):
            with st.spinner("A executar todos os módulos ativos..."):
                bot.run_cycle()
            st.rerun()

    sub_rules, sub_tg, sub_queue, sub_rollback, sub_logs = st.tabs([
        "⚙️ 10 Módulos de Automação",
        "📱 Comandos Remotos Telegram",
        "🗓️ Fila de Publicação",
        "↩️ Histórico & Rollback",
        "📜 Logs em Direto",
    ])

    # --- SUB-ABA 1: CONFIGURAÇÃO DOS 10 MÓDULOS ---
    with sub_rules:
        cfg = bot.config
        with st.form("full_bot_config_form"):
            st.markdown("#### 🔧 Motor & Canais de Notificação")
            m_c1, m_c2, m_c3 = st.columns(3)
            in_interval = m_c1.number_input(
                "Intervalo entre ciclos (min)",
                min_value=2,
                max_value=1440,
                value=int(cfg["interval_minutes"]),
                help="Frequência com que o bot verifica vendas, tráfego e regras (recomendado: 15 min).",
            )
            in_max_act = m_c2.number_input(
                "Máx. alterações por ciclo",
                min_value=1,
                max_value=20,
                value=int(cfg["max_actions_per_cycle"]),
                help="Limite de segurança para evitar alterar demasiados modelos de uma só vez.",
            )
            in_dry = m_c3.checkbox(
                "Modo Simulação (Dry-Run)",
                value=bool(cfg["dry_run"]),
                help="Quando ativo, o bot testa todas as regras e escreve nos logs o que faria, sem alterar nada na loja.",
            )

            n_c1, n_c2, n_c3 = st.columns(3)
            in_tg_tok = n_c1.text_input(
                "Telegram Bot Token",
                value=cfg["tg_token"],
                type="password",
                help="Token HTTP obtido no @BotFather do Telegram.",
            )
            in_tg_chat = n_c2.text_input(
                "Telegram Chat ID",
                value=cfg["tg_chat_id"],
                help="O teu ID numérico do Telegram (obtido no @userinfobot).",
            )
            in_disc = n_c3.text_input(
                "Discord Webhook URL",
                value=cfg["discord_webhook"],
                type="password",
                help="URL opcional de webhook de um canal do Discord para receber alertas.",
            )

            st.divider()
            st.markdown("#### 1️⃣ Vigilante de Vendas, Marcos & Picos de Tráfego")
            s_c1, s_c2, s_c3, s_c4 = st.columns([1, 1, 1, 1])
            in_r_sales = s_c1.checkbox(
                "1. Alertar Vendas",
                value=bool(cfg["rule_sales_alert"]),
                help="Envia recibo imediato no Telegram/Discord sempre que ocorre uma venda.",
            )
            in_r_mile = s_c2.checkbox(
                "2. Celebrar Marcos",
                value=bool(cfg["rule_milestone_alert"]),
                help="Notifica quando uma peça atinge 5, 10, 25, 50 ou 100 vendas.",
            )
            in_r_spike = s_c3.checkbox(
                "3. Picos de Tráfego",
                value=bool(cfg["rule_spike_alert"]),
                help="Avisa quando um modelo ganha muitas views num único ciclo.",
            )
            in_spike_val = s_c4.number_input(
                "Salto mín. views",
                min_value=5,
                max_value=5000,
                value=int(cfg["spike_views_threshold"]),
                help="Número mínimo de novas visualizações entre ciclos para disparar alerta.",
            )

            st.divider()
            st.markdown("#### 4️⃣ Auto-Promo (0 Vendas) & 5️⃣ Reativação de Peças Paradas")
            p_c1, p_c2, p_c3, p_c4, p_c5 = st.columns([1.2, 1, 1, 1, 1])
            in_r_promo = p_c1.checkbox(
                "4. Auto-Promo (0 Vendas)",
                value=bool(cfg["rule_auto_promo"]),
                help="Aplica desconto automático em modelos com muitas views mas ainda sem nenhuma venda.",
            )
            in_p_views = p_c2.number_input("Mín. Views", min_value=10, max_value=50000, value=int(cfg["promo_min_views"]), help="Views mínimas antes de lançar desconto.")
            in_p_pct = p_c3.number_input("Desconto (%)", min_value=5, max_value=90, value=int(cfg["promo_discount_pct"]), help="Percentagem de desconto a aplicar.")
            in_p_days = p_c4.number_input("Duração (dias)", min_value=1, max_value=30, value=int(cfg["promo_days"]), help="Duração da campanha promocional.")
            in_p_cd = p_c5.number_input("Cooldown (dias)", min_value=1, max_value=90, value=int(cfg["promo_cooldown_days"]), help="Dias de pausa obrigatória antes de voltar a promover a mesma peça.")

            st_c1, st_c2, st_c3, st_c4 = st.columns([1.3, 1, 1, 1])
            in_r_stag = st_c1.checkbox(
                "5. Reativar Estagnados",
                value=bool(cfg["rule_stagnant_revival"]),
                help="Lança promoção relâmpago em modelos que já venderam antes mas estão há X dias sem vendas.",
            )
            in_stag_days = st_c2.number_input("Dias sem vender", min_value=7, max_value=365, value=int(cfg["stagnant_days"]), help="Dias desde a última venda para considerar estagnado.")
            in_stag_pct = st_c3.number_input("Desconto Reativação (%)", min_value=5, max_value=90, value=int(cfg["stagnant_discount_pct"]), help="Desconto para reativar a peça.")
            in_stag_pdays = st_c4.number_input("Duração Promo (dias)", min_value=1, max_value=14, value=int(cfg["stagnant_promo_days"]), help="Dias de duração do desconto.")

            st.divider()
            st.markdown("#### 6️⃣ Preço Dinâmico & 7️⃣ Promoção Relâmpago de Fim de Semana")
            d_c1, d_c2, d_c3, d_c4 = st.columns([1.2, 1, 1, 1])
            in_r_price = d_c1.checkbox(
                "6. Preço Dinâmico",
                value=bool(cfg["rule_dynamic_price"]),
                help="Sobe automaticamente o preço base à medida que o modelo acumula vendas.",
            )
            in_d_step = d_c2.number_input("A cada X vendas", min_value=1, max_value=500, value=int(cfg["price_step_sales"]), help="Patamar de vendas para cada subida de preço.")
            in_d_pct = d_c3.number_input("Subida (%)", min_value=5.0, max_value=100.0, value=float(cfg["price_bump_pct"]), step=5.0, help="Percentagem de aumento em cada patamar.")
            in_d_cap = d_c4.number_input(f"Teto Máx ({currency})", min_value=1.0, max_value=500.0, value=float(cfg["price_max_cap"]), step=1.0, help="Preço máximo que o bot nunca ultrapassará.")

            w_c1, w_c2, w_c3, w_c4 = st.columns([1.3, 1, 1, 1])
            in_r_wk = w_c1.checkbox(
                "7. Flash Fim-de-Semana",
                value=bool(cfg["rule_weekend_flash"]),
                help="Aplica automaticamente um desconto de fim de semana todas as semanas no dia/hora escolhidos.",
            )
            dias_opt = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
            in_wk_day = w_c2.selectbox("Dia de início", dias_opt, index=int(cfg["weekend_day"]), help="Dia da semana em que arranca a campanha.")
            in_wk_hr = w_c3.number_input("Hora início (0-23)", min_value=0, max_value=23, value=int(cfg["weekend_hour"]), help="Hora local para ativar o desconto.")
            in_wk_pct = w_c4.number_input("Desconto Flash (%)", min_value=5, max_value=80, value=int(cfg["weekend_discount_pct"]), help="Percentagem de desconto do fim de semana.")

            st.divider()
            st.markdown("#### 8️⃣ Otimizador SEO & Rodapé")
            in_r_seo = st.checkbox(
                "8. Garantir Rodapé & Tags Base em Todos os Modelos",
                value=bool(cfg["rule_seo_optimizer"]),
                help="Adiciona automaticamente o teu rodapé de marca/licença e tags obrigatórias às peças que ainda não os têm.",
            )
            seo_c1, seo_c2 = st.columns(2)
            in_seo_marker = seo_c1.text_input("Marcador único", value=cfg["seo_footer_marker"], help="Texto usado para verificar se o rodapé já existe na descrição.")
            in_seo_tags = seo_c2.text_input("Tags obrigatórias (vírgula)", value=cfg["seo_required_tags"], help="Tags que o bot garante em todas as peças.")
            in_seo_footer = st.text_area("Texto de rodapé a anexar", value=cfg["seo_footer_text"], height=80, help="Texto adicionado ao final da descrição.")

            st.divider()
            st.markdown("#### 9️⃣ Radar de Nichos & 🔟 Relatórios Automáticos")
            rk_c1, rk_c2 = st.columns([1, 2.5])
            in_r_mkt = rk_c1.checkbox(
                "9. Radar de Nichos",
                value=bool(cfg["rule_market_watch"]),
                help="Vigia palavras-chave e avisa quando surgem modelos novos pagos no teu nicho.",
            )
            in_kw = rk_c2.text_input("Palavras-chave (máx. 4, vírgula)", value=cfg["watch_keywords"], placeholder="seat leon, gauge pod, intake", help="Termos pesquisados automaticamente a cada ciclo.")

            rep_c1, rep_c2, rep_c3, rep_c4 = st.columns([1.1, 0.9, 1.1, 1.1])
            in_r_rep = rep_c1.checkbox("10a. Relatório Diário", value=bool(cfg["rule_daily_report"]), help="Envia resumo diário de vendas e novas views no Telegram/Discord.")
            in_rep_hr = rep_c2.number_input("Hora envio (0-23)", min_value=0, max_value=23, value=int(cfg["daily_report_hour"]), help="Hora local para envio do relatório diário.")
            in_r_wk_rep = rep_c3.checkbox("10b. Resumo Semanal", value=bool(cfg["rule_weekly_report"]), help="Envia resumo semanal aos domingos às 20h.")
            in_r_queue = rep_c4.checkbox("Fila Publicação", value=bool(cfg["rule_queue_publisher"]), help="Publica automaticamente modelos agendados na fila.")

            save_all_btn = st.form_submit_button("💾 Guardar Toda a Configuração", type="primary", use_container_width=True)

        if save_all_btn:
            with bot._lock:
                bot.config.update({
                    "interval_minutes": int(in_interval),
                    "max_actions_per_cycle": int(in_max_act),
                    "dry_run": bool(in_dry),
                    "tg_token": in_tg_tok.strip(),
                    "tg_chat_id": in_tg_chat.strip(),
                    "discord_webhook": in_disc.strip(),
                    "rule_sales_alert": bool(in_r_sales),
                    "rule_milestone_alert": bool(in_r_mile),
                    "rule_spike_alert": bool(in_r_spike),
                    "spike_views_threshold": int(in_spike_val),
                    "rule_auto_promo": bool(in_r_promo),
                    "promo_min_views": int(in_p_views),
                    "promo_discount_pct": int(in_p_pct),
                    "promo_days": int(in_p_days),
                    "promo_cooldown_days": int(in_p_cd),
                    "rule_stagnant_revival": bool(in_r_stag),
                    "stagnant_days": int(in_stag_days),
                    "stagnant_discount_pct": int(in_stag_pct),
                    "stagnant_promo_days": int(in_stag_pdays),
                    "rule_dynamic_price": bool(in_r_price),
                    "price_step_sales": int(in_d_step),
                    "price_bump_pct": float(in_d_pct),
                    "price_max_cap": float(in_d_cap),
                    "rule_weekend_flash": bool(in_r_wk),
                    "weekend_day": dias_opt.index(in_wk_day),
                    "weekend_hour": int(in_wk_hr),
                    "weekend_discount_pct": int(in_wk_pct),
                    "rule_seo_optimizer": bool(in_r_seo),
                    "seo_footer_marker": in_seo_marker.strip(),
                    "seo_required_tags": in_seo_tags.strip(),
                    "seo_footer_text": in_seo_footer,
                    "rule_market_watch": bool(in_r_mkt),
                    "watch_keywords": in_kw.strip(),
                    "rule_daily_report": bool(in_r_rep),
                    "daily_report_hour": int(in_rep_hr),
                    "rule_weekly_report": bool(in_r_wk_rep),
                    "rule_queue_publisher": bool(in_r_queue),
                })
            bot.log("CONFIG", "Configuração completa do bot guardada.")
            st.success("Configuração guardada com sucesso!")

        c_t1, c_t2 = st.columns(2)
        if c_t1.button("🔔 Enviar Notificação de Teste", use_container_width=True, help="Envia uma mensagem imediata para confirmar que o Telegram/Discord está ligado."):
            bot.send_notification("✅ <b>Cults3D Studio Pro:</b> Ligação Telegram/Discord operacional!")
            st.toast("Notificação enviada!")
        if c_t2.button("🧹 Limpar Cooldowns de Promoção", use_container_width=True, help="Permite que o bot volte a avaliar modelos que estavam em pausa promocional."):
            with bot._lock:
                bot.promo_cooldowns = {}
            bot._save_state()
            st.toast("Cooldowns limpos!")

    # --- SUB-ABA 2: COMANDOS REMOTOS VIA TELEGRAM ---
    with sub_tg:
        st.markdown("#### 📱 Controla a Tua Loja por Mensagem no Telegram")
        st.write(
            "Quando o bot está ativo no PC, podes enviar comandos diretamente no chat com o teu bot do Telegram (apenas o teu `Chat ID` tem permissão para executar comandos):"
        )
        st.markdown(
            """
            - `/status` — Mostra se o bot está ativo, modo (Simulação/Real), último ciclo e quota diária da API.
            - `/vendas` — Devolve as últimas 5 vendas com valor líquido e comprador.
            - `/top` — Lista os teus 5 modelos mais vistos e respetivos downloads.
            - `/ciclo` — Força a execução imediata de um ciclo completo de verificação.
            - `/pausar` — Pausa o bot remotamente a partir do telemóvel.
            - `/retomar` — Volta a ligar o bot remotamente.
            """
        )
        if st.button("📡 Verificar Comandos Pendentes Agora", help="Lê imediatamente as últimas mensagens enviadas ao bot no Telegram."):
            bot.poll_telegram_commands()
            st.toast("Verificação de comandos concluída!")

    # --- SUB-ABA 3: FILA DE PUBLICAÇÃO AGENDADA ---
    with sub_queue:
        st.markdown("#### 🗓️ Fila de Publicação Automática (Drip-Feed)")
        if bot.publish_queue:
            q_df = pd.DataFrame([
                {
                    "ID": x["queue_id"],
                    "Modelo": x["name"],
                    "Preço": x["price"],
                    "Agendado (UTC)": x["scheduled_at"],
                    "Estado": x["status"],
                }
                for x in bot.publish_queue
            ])
            st.dataframe(q_df, use_container_width=True, hide_index=True)

            if st.button("🗑️ Limpar Itens Já Publicados / Com Erro", help="Remove da lista os modelos que já foram publicados."):
                with bot._lock:
                    bot.publish_queue = [x for x in bot.publish_queue if x.get("status") == "Pendente"]
                bot._save_state()
                st.rerun()
        else:
            st.info("A fila está vazia. Usa a aba **🚀 Estúdio Publicação & SEO** e clica em **Agendar na Fila do Bot**.")

    # --- SUB-ABA 4: HISTÓRICO & ROLLBACK ---
    with sub_rollback:
        st.markdown("#### ↩️ Auditoria de Alterações & Reversão")
        with bot._lock:
            hist_items = list(bot.action_history)

        if not hist_items:
            st.info("O bot ainda não executou alterações reais na conta.")
        else:
            h_df = pd.DataFrame([
                {
                    "Hora": h["hora"],
                    "Tipo": h["tipo"],
                    "Modelo": h["modelo"],
                    "Detalhe": h["detalhe"],
                    "Revertido": "Sim" if h.get("reverted") else "Não",
                }
                for h in hist_items
            ])
            st.dataframe(h_df, use_container_width=True, hide_index=True)

            reversible_list = [h for h in hist_items if h.get("reversible") and not h.get("reverted")]
            if reversible_list:
                rev_map = {
                    f"[{h['hora']}] {h['tipo']} — {h['modelo']} ({h['detalhe']})": h["action_id"]
                    for h in reversible_list
                }
                sel_rev = st.selectbox("Selecionar ação para desfazer", list(rev_map.keys()))
                if st.button("↩️ Desfazer Alteração Selecionada", type="primary"):
                    try:
                        msg = bot.revert_action(rev_map[sel_rev])
                        st.success(msg)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    # --- SUB-ABA 5: LOGS ---
    with sub_logs:
        l1, l2 = st.columns([1, 1])
        if l1.button("🔄 Atualizar Logs", use_container_width=True):
            st.rerun()
        if l2.button("🧹 Limpar Logs", use_container_width=True):
            with bot._lock:
                bot.logs = []
            bot._save_state()
            st.rerun()

        with bot._lock:
            current_logs = list(bot.logs)

        if current_logs:
            st.dataframe(pd.DataFrame(current_logs), use_container_width=True, hide_index=True)
        else:
            st.info("Sem registos nos logs.")


# ==============================================================================
# ABA 3: ESTÚDIO DE PUBLICAÇÃO & SEO (GAMAS, TEMPLATES & SCORE)
# ==============================================================================
with tab_publish:
    st.subheader("🚀 Estúdio Profissional de Publicação & SEO")
    st.caption("Usa Presets de Gama para preencher definições de impressão e licença em 1 clique, valida o SEO Score e publica agora ou agenda.")

    # Gestor de Presets / Gamas
    with bot._lock:
        presets_dict = dict(bot.presets)

    pr_c1, pr_c2 = st.columns([2, 1])
    selected_preset_name = pr_c1.selectbox(
        "Selecionar Gama / Template de Produto",
        ["(Personalizado / Em branco)"] + list(presets_dict.keys()),
        help="Carrega automaticamente definições de impressão, preço sugerido, licença e tags base para a gama escolhida.",
    )
    active_preset = presets_dict.get(selected_preset_name, {})

    cats = st.session_state.categories
    cat_map = {c["label"]: c["id"] for c in cats} if cats else {}

    col_form, col_preview = st.columns([1.4, 1])

    with col_form:
        p_name = st.text_input(
            "Título do Modelo *",
            placeholder="Ex: Seat Leon Mk2 (1P) - 52mm Air Vent Turbo Gauge Pod",
            help="Inclui modelo exato, dimensões e função principal para captar pesquisas exatas.",
        )

        c1, c2, c3 = st.columns(3)
        if cat_map:
            default_cat_idx = 0
            hint = str(active_preset.get("category_hint", "")).lower()
            if hint:
                for idx_c, lbl_c in enumerate(cat_map.keys()):
                    if hint in lbl_c.lower():
                        default_cat_idx = idx_c
                        break
            selected_cat_label = c1.selectbox(
                "Categoria *",
                list(cat_map.keys()),
                index=default_cat_idx,
                help="Categoria oficial do Cults3D onde o modelo será listado.",
            )
            p_cat_id = cat_map[selected_cat_label]
        else:
            p_cat_id = c1.text_input("ID da Categoria *", placeholder="ID numérico")

        p_price = c2.number_input(
            f"Preço ({currency})",
            min_value=0.0,
            step=0.5,
            value=float(active_preset.get("price", 2.50)),
            help="Define 0.00 para modelo gratuito.",
        )
        p_locale = c3.selectbox(
            "Idioma",
            ["EN", "PT", "ES", "FR", "DE"],
            index=0,
            help="Idioma principal do anúncio (EN maximiza vendas internacionais).",
        )

        st.markdown("**Construtor Estruturado de Descrição**")
        d_overview = st.text_area(
            "1. Resumo & Funcionalidades *",
            value=active_preset.get("overview", ""),
            height=80,
            help="Explica o que a peça faz, compatibilidade e vantagens.",
        )
        d_settings = st.text_area(
            "2. Definições de Impressão 3D *",
            value=active_preset.get("print_settings", ""),
            height=110,
            help="Material (PETG/ABS/ASA/PLA), altura de camada, paredes, infill e suportes.",
        )
        d_assembly = st.text_area(
            "3. Montagem & Hardware (opcional)",
            value=active_preset.get("assembly", ""),
            height=65,
            help="Parafusos, cola, encaixes ou dicas de pós-processamento.",
        )
        d_license = st.text_input(
            "4. Aviso de Licença",
            value=active_preset.get("license", "Personal use only."),
            help="Direitos de utilização comercial ou pessoal.",
        )

        p_tags = st.text_input(
            "Tags SEO (separadas por vírgula) *",
            value=active_preset.get("tags", ""),
            help="Usa entre 8 e 15 palavras-chave separadas por vírgula.",
        )

        p_files = st.text_area(
            "URLs dos Ficheiros 3D (.stl / .3mf / .zip — 1 por linha, HTTPS) *",
            placeholder="https://.../peca_principal.stl",
            height=75,
            help="Links diretos HTTPS públicos (máx. 10 ficheiros).",
        )
        p_images = st.text_area(
            "URLs das Imagens / Renders (1 por linha, HTTPS) *",
            placeholder="https://.../render1.jpg",
            height=75,
            help="Links diretos HTTPS das fotos/renders (máx. 10 imagens; a primeira será a capa).",
        )

    # Montar descrição final estruturada e calcular SEO em tempo real
    sections = [d_overview.strip()]
    if d_settings.strip():
        sections.append(f"### Print Settings\n{d_settings.strip()}")
    if d_assembly.strip():
        sections.append(f"### Assembly & Notes\n{d_assembly.strip()}")
    if d_license.strip():
        sections.append(f"### License\n{d_license.strip()}")
    full_description = "\n\n".join(s for s in sections if s)

    tag_list = [t.strip() for t in p_tags.split(",") if t.strip()]
    seo_report = analyze_listing_seo(p_name, full_description, tag_list, p_price)

    with col_preview:
        st.markdown("#### 🎯 Diagnóstico SEO em Tempo Real")
        score_val = seo_report["score"]
        st.progress(score_val / 100.0, text=f"Qualidade SEO do Anúncio: {score_val}/100")

        if seo_report["suggested_tags"]:
            st.info(
                "**Tags sugeridas a partir do título:** `"
                + ", ".join(seo_report["suggested_tags"])
                + "`"
            )

        if seo_report["tips"]:
            for tip in seo_report["tips"]:
                st.caption(f"⚠️ {tip}")
        else:
            st.success("✅ Anúncio otimizado para máxima conversão e pesquisa!")

        st.markdown("#### 👁️ Pré-visualização da Descrição")
        st.markdown(f"<div class='seo-card'>{full_description.replace(chr(10), '<br>')}</div>", unsafe_allow_html=True)

        # Guardar novo preset de gama
        with st.expander("💾 Guardar como Nova Gama / Template"):
            new_preset_title = st.text_input("Nome da Gama", placeholder="Ex: Peças Seat Leon Mk2")
            if st.button("Guardar Preset", use_container_width=True):
                if new_preset_title.strip():
                    with bot._lock:
                        bot.presets[new_preset_title.strip()] = {
                            "category_hint": "",
                            "price": float(p_price),
                            "locale": p_locale,
                            "tags": p_tags,
                            "overview": d_overview,
                            "print_settings": d_settings,
                            "assembly": d_assembly,
                            "license": d_license,
                        }
                    bot._save_state()
                    st.success(f"Gama '{new_preset_title.strip()}' guardada!")
                    st.rerun()

    st.divider()
    act_p1, act_p2, act_p3 = st.columns([1.2, 1, 1.2])
    sched_hours = act_p2.number_input(
        "Atraso para agendamento (horas)",
        min_value=1,
        max_value=720,
        value=2,
        help="Usado apenas se clicares em 'Agendar na Fila do Bot'.",
    )

    file_list = [u.strip() for u in p_files.splitlines() if u.strip()]
    img_list = [u.strip() for u in p_images.splitlines() if u.strip()]

    if act_p1.button("🚀 Publicar Agora no Cults3D", type="primary", use_container_width=True):
        if not p_name.strip() or not full_description.strip() or not p_cat_id or not file_list or not img_list:
            st.warning("Preenche todos os campos obrigatórios (*).")
        elif len(file_list) > 10 or len(img_list) > 10:
            st.warning("Máximo de 10 ficheiros 3D e 10 imagens por modelo.")
        elif not all(u.startswith("https://") for u in file_list + img_list):
            st.warning("Todos os URLs têm de começar por https://")
        else:
            with st.spinner("A enviar ficheiros e a publicar no Cults3D..."):
                try:
                    created = client.create_creation(
                        name=p_name.strip(),
                        description=full_description,
                        category_id=str(p_cat_id),
                        file_urls=file_list,
                        image_urls=img_list,
                        price=p_price,
                        currency=currency,
                        tags=tag_list,
                        locale=p_locale,
                    )
                    st.session_state.creations = client.get_all_my_creations(currency=currency)
                    st.success(f"Modelo publicado com sucesso! Link: {created.get('url')}")
                except Exception as e:
                    st.error(str(e))

    if act_p3.button("🗓️ Agendar na Fila do Bot", use_container_width=True, help="Coloca este modelo na fila do bot para ser publicado automaticamente à hora agendada."):
        if not p_name.strip() or not full_description.strip() or not p_cat_id or not file_list or not img_list:
            st.warning("Preenche todos os campos obrigatórios (*).")
        elif not all(u.startswith("https://") for u in file_list + img_list):
            st.warning("Todos os URLs têm de começar por https://")
        else:
            sched_dt = datetime.now(timezone.utc) + timedelta(hours=int(sched_hours))
            with bot._lock:
                bot.publish_queue.append({
                    "queue_id": f"q_{int(time.time() * 1000)}",
                    "name": p_name.strip(),
                    "description": full_description,
                    "category_id": str(p_cat_id),
                    "price": float(p_price),
                    "locale": p_locale,
                    "tags": tag_list,
                    "file_urls": file_list,
                    "image_urls": img_list,
                    "scheduled_at": sched_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "Pendente",
                })
            bot._save_state()
            st.success(f"Agendado para {sched_dt.strftime('%Y-%m-%d %H:%M UTC')}!")


# ==============================================================================
# ABA 4: GESTÃO EM MASSA & CAMPANHAS
# ==============================================================================
with tab_bulk:
    if df_creations.empty:
        st.info("Não tens modelos carregados para editar.")
    else:
        st.subheader("⚡ Edição em Massa & Campanhas Promocionais")
        options_map = {
            f"{row['nome']} ({row['preco']:.2f} {currency} | {row['views']} views)": row
            for _, row in df_creations.iterrows()
        }
        all_labels = list(options_map.keys())

        select_all = st.checkbox("Selecionar todos os modelos", help="Marca rapidamente todo o catálogo.")
        selected_labels = st.multiselect(
            "Modelos selecionados",
            all_labels,
            default=all_labels if select_all else [],
            help="Escolhe as peças onde queres aplicar alterações em lote.",
        )

        st.divider()
        col_act1, col_act2, col_act3 = st.columns(3)

        with col_act1:
            st.markdown("### 🏷️ Preços Base")
            price_mode = st.radio(
                "Modo de ajuste",
                ["Ajuste Percentual (%)", "Preço Fixo Novo"],
                help="Percentual ajusta proporcionalmente; Preço Fixo define o mesmo valor em todos.",
            )
            price_val = st.number_input("Valor (ex: -20 ou 4.99)", value=0.0, step=0.5, help="Usa valores negativos para reduzir preços.")

            if st.button("Aplicar Preços", type="primary", use_container_width=True):
                if not selected_labels:
                    st.warning("Seleciona pelo menos um modelo.")
                else:
                    progress = st.progress(0)
                    errors_count = 0
                    for idx, label in enumerate(selected_labels):
                        row = options_map[label]
                        old_p = row["preco"]
                        new_p = (
                            round(old_p * (1.0 + price_val / 100.0), 2)
                            if price_mode == "Ajuste Percentual (%)"
                            else round(price_val, 2)
                        )
                        try:
                            client.update_creation(creation_id=row["id"], price=max(0.0, new_p), currency=currency)
                        except Exception as e:
                            errors_count += 1
                            st.error(f"Erro em {row['nome']}: {e}")
                        progress.progress((idx + 1) / len(selected_labels))
                    if errors_count == 0:
                        st.session_state.creations = client.get_all_my_creations(currency=currency)
                        st.success("Preços atualizados!")
                        st.rerun()

        with col_act2:
            st.markdown("### 🔥 Campanha de Desconto")
            disc_pct = st.slider("Desconto (%)", min_value=5, max_value=90, value=20, step=5, help="Percentagem de desconto promocional visível no site.")
            disc_days = st.number_input("Duração (dias)", min_value=1, max_value=90, value=7, help="Número de dias até o desconto expirar automaticamente.")

            if st.button("Ativar Desconto", type="primary", use_container_width=True):
                if not selected_labels:
                    st.warning("Seleciona pelo menos um modelo.")
                else:
                    end_at = (datetime.now(timezone.utc) + timedelta(days=int(disc_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
                    progress = st.progress(0)
                    errors_count = 0
                    for idx, label in enumerate(selected_labels):
                        row = options_map[label]
                        try:
                            client.create_discount(
                                creation_id=row["id"],
                                percentage=disc_pct,
                                end_at_iso=end_at,
                            )
                        except Exception as e:
                            errors_count += 1
                            st.error(f"Erro em {row['nome']}: {e}")
                        progress.progress((idx + 1) / len(selected_labels))
                    if errors_count == 0:
                        st.success(f"Desconto de {disc_pct}% ativo por {int(disc_days)} dias!")

        with col_act3:
            st.markdown("### 🔖 Tags & SEO")
            tag_mode = st.radio(
                "Ação nas tags",
                ["Adicionar às existentes", "Substituir todas"],
                help="Adicionar mantém as tags atuais e acrescenta as novas.",
            )
            tags_input = st.text_input("Tags (vírgula)", placeholder="ex: stl, 3d print, tuning", help="Tags separadas por vírgula.")

            if st.button("Atualizar Tags", type="primary", use_container_width=True):
                new_tags = [t.strip() for t in tags_input.split(",") if t.strip()]
                if not selected_labels or not new_tags:
                    st.warning("Seleciona modelos e escreve pelo menos uma tag.")
                else:
                    progress = st.progress(0)
                    errors_count = 0
                    for idx, label in enumerate(selected_labels):
                        row = options_map[label]
                        if tag_mode == "Adicionar às existentes":
                            merged = list(dict.fromkeys(row["tags_list"] + new_tags))
                        else:
                            merged = new_tags
                        try:
                            client.update_creation(creation_id=row["id"], tags=merged)
                        except Exception as e:
                            errors_count += 1
                            st.error(f"Erro em {row['nome']}: {e}")
                        progress.progress((idx + 1) / len(selected_labels))
                    if errors_count == 0:
                        st.session_state.creations = client.get_all_my_creations(currency=currency)
                        st.success("Tags atualizadas!")
                        st.rerun()


# ==============================================================================
# ABA 5: RADAR DE MERCADO & INTELIGÊNCIA DE NICHOS (CORRIGIDO)
# ==============================================================================
with tab_market:
    st.subheader("🔍 Radar de Mercado & Inteligência de Nichos")
    st.caption("Analisa concorrência real via `creationsSearchBatch`, estima receita por modelo, descobre preços ideais e extrai as melhores keywords.")

    r_c1, r_c2, r_c3, r_c4 = st.columns([2.5, 1.2, 1, 1])
    m_query = r_c1.text_input(
        "Palavra-chave / Nicho a investigar *",
        placeholder="Ex: seat leon, gauge pod, articulated dragon, deck box",
        help="Termo exato pesquisado no motor de busca do Cults3D.",
    )
    m_price_filter = r_c2.selectbox(
        "Tipo de Modelos",
        ["Apenas Pagos", "Todos (Pagos + Grátis)", "Apenas Gratuitos"],
        help="Filtra os modelos analisados por modelo de monetização.",
    )
    m_limit = r_c3.number_input(
        "Amostra (modelos)",
        min_value=10,
        max_value=100,
        value=40,
        step=10,
        help="Quantidade de resultados a recolher (usa paginação automática acima de 50).",
    )
    m_sort = r_c4.selectbox(
        "Ordenar por",
        ["Receita Estimada", "Downloads", "Likes", "Views", "Preço"],
        help="Critério de ordenação da tabela de concorrência.",
    )

    if st.button("🔎 Analisar Nicho Agora", type="primary", help="Executa pesquisa em tempo real na API do Cults3D e calcula indicadores de oportunidade."):
        if not m_query.strip():
            st.warning("Escreve um termo de pesquisa.")
        else:
            with st.spinner(f"A investigar o nicho '{m_query.strip()}' no Cults3D..."):
                try:
                    only_paid_flag = m_price_filter == "Apenas Pagos"
                    raw_search = client.search_market(
                        search_query=m_query.strip(),
                        limit=int(m_limit),
                        only_priced=only_paid_flag,
                        currency=currency,
                    )
                    total_in_cults = raw_search.get("total", 0)
                    results = raw_search.get("results") or []

                    rows = []
                    all_niche_tags: List[str] = []
                    for r in results:
                        p_cents = (r.get("price") or {}).get("cents") or 0
                        p_val = round(p_cents / 100.0, 2)
                        if m_price_filter == "Apenas Gratuitos" and p_val > 0:
                            continue

                        v_cnt = int(r.get("viewsCount") or 0)
                        l_cnt = int(r.get("likesCount") or 0)
                        d_cnt = int(r.get("downloadsCount") or 0)
                        est_rev = round(p_val * d_cnt, 2)
                        conv_dl = round((d_cnt / v_cnt * 100.0), 2) if v_cnt > 0 else 0.0
                        r_tags = [str(t).strip().lower() for t in (r.get("tags") or []) if str(t).strip()]
                        all_niche_tags.extend(r_tags)

                        rows.append({
                            "nome": r.get("name") or "",
                            "criador": (r.get("creator") or {}).get("nick") or "?",
                            "preco": p_val,
                            "views": v_cnt,
                            "likes": l_cnt,
                            "downloads": d_cnt,
                            "receita_est": est_rev,
                            "conv_dl_%": conv_dl,
                            "url": r.get("url") or "",
                        })

                    if not rows:
                        st.info("Nenhum modelo encontrado com esses filtros.")
                    else:
                        df_m = pd.DataFrame(rows)
                        paid_subset = df_m[df_m["preco"] > 0]

                        avg_price = float(paid_subset["preco"].mean()) if not paid_subset.empty else 0.0
                        med_price = float(paid_subset["preco"].median()) if not paid_subset.empty else 0.0
                        avg_dl = float(df_m["downloads"].mean())
                        total_est_rev = float(df_m["receita_est"].sum())

                        # Índice de Oportunidade (0 a 100): procura alta vs concorrência moderada
                        demand_pts = min(60.0, avg_dl * 1.5 + (total_est_rev / max(1, len(df_m))) * 0.5)
                        comp_penalty = min(40.0, (total_in_cults / 500.0) * 40.0)
                        opp_score = int(max(10, min(100, round(demand_pts + (40.0 - comp_penalty)))))

                        mk1, mk2, mk3, mk4, mk5 = st.columns(5)
                        mk1.metric(
                            "Resultados Totais",
                            f"{total_in_cults:,}",
                            help="Total de peças existentes no Cults3D para esta pesquisa (mede a saturação).",
                        )
                        mk2.metric(
                            "Índice Oportunidade",
                            f"{opp_score}/100",
                            help="Pontuação que cruza procura (downloads/receita) com saturação de concorrentes.",
                        )
                        mk3.metric(
                            f"Preço Médio / Mediana",
                            f"{avg_price:.2f} / {med_price:.2f} {currency}",
                            help="Preço médio e mediano dos modelos pagos neste nicho.",
                        )
                        mk4.metric(
                            "Média Downloads",
                            f"{avg_dl:.1f}",
                            help="Média de downloads por peça na amostra analisada.",
                        )
                        mk5.metric(
                            f"Receita Est. Amostra",
                            f"{total_est_rev:,.2f} {currency}",
                            help="Soma estimada (Preço × Downloads) das peças analisadas.",
                        )

                        st.divider()
                        ch_c1, ch_c2 = st.columns([1.4, 1])

                        with ch_c1:
                            st.markdown("#### 📊 Downloads por Faixa de Preço")
                            if not paid_subset.empty:
                                chart_df = paid_subset[["preco", "downloads"]].groupby("preco")["downloads"].mean()
                                st.bar_chart(chart_df)
                            else:
                                st.caption("Sem modelos pagos na amostra para gerar gráfico de preços.")

                        with ch_c2:
                            st.markdown("#### 🏷️ Top Keywords & Concorrentes")
                            if all_niche_tags:
                                top_tags = Counter(all_niche_tags).most_common(12)
                                st.write("**Tags mais usadas pelos líderes:**")
                                st.code(", ".join(f"{t} ({cnt})" for t, cnt in top_tags), language=None)

                            top_creators = (
                                df_m.groupby("criador")
                                .agg(pecas=("nome", "count"), downloads=("downloads", "sum"), receita_est=("receita_est", "sum"))
                                .sort_values(by="downloads", ascending=False)
                                .head(5)
                            )
                            st.write("**Top Criadores neste Nicho:**")
                            st.dataframe(top_creators, use_container_width=True)

                        sort_map = {
                            "Receita Estimada": "receita_est",
                            "Downloads": "downloads",
                            "Likes": "likes",
                            "Views": "views",
                            "Preço": "preco",
                        }
                        st.markdown("#### 📋 Modelos Encontrados")
                        st.dataframe(
                            df_m.sort_values(by=sort_map[m_sort], ascending=False),
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "nome": "Modelo",
                                "criador": "Criador",
                                "preco": st.column_config.NumberColumn(f"Preço ({currency})", format="%.2f"),
                                "receita_est": st.column_config.NumberColumn(f"Receita Est. ({currency})", format="%.2f"),
                                "conv_dl_%": st.column_config.NumberColumn("DL/Views (%)", format="%.2f%%"),
                                "url": st.column_config.LinkColumn("Link", display_text="Ver"),
                            },
                        )
                except Exception as e:
                    st.error(str(e))


# ==============================================================================
# ABA 6: CONSOLA GRAPHQL & DIAGNÓSTICO DE SCHEMA
# ==============================================================================
with tab_console:
    st.subheader("🛠️ Consola GraphQL & Diagnóstico Técnico")
    col_g1, col_g2 = st.columns([2, 1])

    with col_g1:
        raw_query = st.text_area(
            "Query ou Mutation GraphQL",
            value="query {\n  myself {\n    user { nick url }\n    creationsBatch(limit: 5) {\n      total\n      results { id name viewsCount }\n    }\n  }\n}",
            height=190,
            help="Permite executar qualquer pedido GraphQL diretamente contra o endpoint do Cults3D.",
        )
        raw_vars = st.text_input("Variables (JSON)", value="{}", help="Variáveis JSON opcionais para a query.")
        if st.button("▶️ Executar Pedido"):
            try:
                res = client.execute(raw_query, json.loads(raw_vars) if raw_vars.strip() else {})
                st.json(res)
            except Exception as e:
                st.error(str(e))

    with col_g2:
        st.markdown("**Inspecionar Schema da API**")
        type_to_check = st.text_input(
            "Tipo (ex: Mutation, Query, Creation)",
            value="Mutation",
            help="Mostra todos os campos e argumentos aceites por qualquer tipo da API.",
        )
        if st.button("🔎 Inspecionar"):
            try:
                st.json(client.introspect_type(type_to_check.strip()))
            except Exception as e:
                st.error(str(e))