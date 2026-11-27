from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Cults3D Studio Pro v5 — Growth & SEO",
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
        padding-top: 1.3rem;
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
        padding: 8px 14px;
        font-weight: 600;
    }
    .seo-card {
        background-color: rgba(128, 128, 128, 0.07);
        border-left: 4px solid #00a8e8;
        border-radius: 8px;
        padding: 14px 18px;
        margin-bottom: 12px;
    }
    .upload-badge {
        background-color: rgba(0, 168, 232, 0.12);
        border: 1px solid rgba(0, 168, 232, 0.35);
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 0.85rem;
        margin-bottom: 6px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==============================================================================
# UTILITÁRIOS GERAIS, SECRETS & PERSISTÊNCIA
# ==============================================================================
class CultsAPIError(Exception):
    pass


def get_secret_val(key: str, default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if val:
        return val
    try:
        if key in st.secrets:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return default


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


def next_golden_hour_utc() -> datetime:
    """Calcula a próxima Hora de Ouro (Sexta 18:00 ou Domingo 15:00 hora local) em UTC."""
    now_local = datetime.now().astimezone()
    candidates: List[datetime] = []
    for days_ahead in range(0, 8):
        day_dt = now_local + timedelta(days=days_ahead)
        if day_dt.weekday() == 4:
            cand = day_dt.replace(hour=18, minute=0, second=0, microsecond=0)
            if cand > now_local + timedelta(minutes=15):
                candidates.append(cand)
        elif day_dt.weekday() == 6:
            cand = day_dt.replace(hour=15, minute=0, second=0, microsecond=0)
            if cand > now_local + timedelta(minutes=15):
                candidates.append(cand)
    chosen = min(candidates) if candidates else (now_local + timedelta(hours=2))
    return chosen.astimezone(timezone.utc)


# ==============================================================================
# CLOUD ASSET UPLOADER (PONTE DRAG & DROP -> HTTPS DIRETO PARA CULTS3D)
# ==============================================================================
class CloudAssetUploader:
    """Carrega ficheiros locais (imagens e STL/3MF) para alojamento HTTPS direto
    para que o Cults3D os importe automaticamente via GraphQL sem configuração externa.
    """

    @staticmethod
    def upload_bytes(file_bytes: bytes, filename: str, mime_type: str = "application/octet-stream") -> str:
        errors: List[str] = []

        # 1. Catbox.moe (Link direto permanente até 200MB)
        try:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (filename, file_bytes, mime_type)},
                timeout=45,
            )
            if resp.status_code == 200:
                url = resp.text.strip()
                if url.startswith("https://"):
                    return url
            errors.append(f"Catbox ({resp.status_code})")
        except Exception as exc:
            errors.append(f"Catbox ({exc})")

        # 2. Litterbox (Link direto 24h — suficiente para ingestão imediata pelo Cults3D)
        try:
            resp = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "24h"},
                files={"fileToUpload": (filename, file_bytes, mime_type)},
                timeout=45,
            )
            if resp.status_code == 200:
                url = resp.text.strip()
                if url.startswith("https://"):
                    return url
            errors.append(f"Litterbox ({resp.status_code})")
        except Exception as exc:
            errors.append(f"Litterbox ({exc})")

        # 3. Tmpfiles.org (Fallback com conversão para link /dl/)
        try:
            resp = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": (filename, file_bytes, mime_type)},
                timeout=45,
            )
            if resp.status_code == 200:
                data = resp.json()
                raw_url = ((data.get("data") or {}).get("url") or "").strip()
                if raw_url.startswith("http"):
                    direct = raw_url.replace("http://", "https://").replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                    return direct
            errors.append(f"Tmpfiles ({resp.status_code})")
        except Exception as exc:
            errors.append(f"Tmpfiles ({exc})")

        raise CultsAPIError(f"Falha ao hospedar '{filename}' na cloud: {', '.join(errors)}")


# ==============================================================================
# MOTOR DE SEO & OTIMIZAÇÃO DE CONVERSÃO (20 TAGS + DUPLA UNIDADE + AUDITORIA)
# ==============================================================================
class SeoOptimizerEngine:
    STOPWORDS = {
        "the", "and", "for", "with", "from", "into", "that", "this", "your",
        "para", "com", "sem", "por", "uma", "uns", "das", "dos", "que",
        "file", "files", "download", "model", "part", "parts", "piece",
    }

    BASE_3D_TAGS = [
        "3d print", "stl", "functional print", "3d printing", "replacement part",
        "custom part", "diy", "maker", "3d model", "printable",
    ]

    @staticmethod
    def mm_to_imperial_hint(mm_val: float) -> str:
        inches = mm_val / 25.4
        if inches < 1.0:
            frac = Fraction(inches).limit_denominator(16)
            if frac.numerator > 0 and abs(float(frac) - inches) <= 0.025:
                return f'{frac.numerator}/{frac.denominator}"'
        return f'{inches:.2f}"'

    @classmethod
    def enrich_metric_units(cls, text: str) -> str:
        """Adiciona equivalência em polegadas junto a medidas em mm (ex: 8mm -> 8mm (5/16\")) se ainda não existir."""
        if not text:
            return text

        def _repl(match: re.Match) -> str:
            full = match.group(0)
            num_str = match.group(1).replace(",", ".")
            after = text[match.end(): match.end() + 14]
            if '"' in after or "inch" in after.lower() or "in)" in after.lower():
                return full
            try:
                val = float(num_str)
                if 1.0 <= val <= 500.0:
                    imp = cls.mm_to_imperial_hint(val)
                    return f"{full} ({imp})"
            except ValueError:
                pass
            return full

        return re.sub(r"\b(\d+(?:[.,]\d+)?)\s?mm\b", _repl, text, flags=re.IGNORECASE)

    @classmethod
    def extract_priority_keywords(cls, title: str, description: str = "") -> List[str]:
        """Extrai termos de alto impacto do título (peso 2x) e descrição para liderarem as tags."""
        title_tokens = [
            w for w in re.findall(r"[a-z0-9À-ÿ\-]{2,}", title.lower())
            if w not in cls.STOPWORDS and not w.isdigit()
        ]
        bigrams: List[str] = []
        if len(title_tokens) >= 2:
            for i in range(len(title_tokens) - 1):
                bg = f"{title_tokens[i]} {title_tokens[i + 1]}"
                bigrams.append(bg)

        desc_tokens = [
            w for w in re.findall(r"[a-z0-9À-ÿ]{3,}", description.lower())
            if w not in cls.STOPWORDS and not w.isdigit()
        ]
        common_desc = [w for w, _ in Counter(desc_tokens).most_common(12)]

        ordered: List[str] = []
        for item in bigrams[:4] + title_tokens + common_desc:
            clean = item.strip("- ").lower()
            if len(clean) >= 2 and clean not in ordered:
                ordered.append(clean)
        return ordered

    @classmethod
    def build_20_tags(
        cls,
        title: str,
        description: str,
        existing_tags: List[str],
        extra_priority_tags: Optional[List[str]] = None,
        max_tags: int = 20,
    ) -> List[str]:
        """Constrói até 20 tags ordenadas por relevância (Top 5 com palavras do título)."""
        priority_from_title = cls.extract_priority_keywords(title, description)
        clean_existing = [t.strip().lower() for t in existing_tags if t.strip()]
        clean_extra = [t.strip().lower() for t in (extra_priority_tags or []) if t.strip()]

        # Ordem de peso: Tags extra prioritárias -> Keywords do título -> Tags atuais -> Base 3D
        pool = clean_extra + priority_from_title + clean_existing + cls.BASE_3D_TAGS
        final_tags: List[str] = []
        for tag in pool:
            if tag and tag not in final_tags:
                final_tags.append(tag)
            if len(final_tags) >= max_tags:
                break
        return final_tags

    @classmethod
    def analyze_listing(cls, name: str, description: str, tags: List[str], price: float) -> Dict[str, Any]:
        score = 0
        tips: List[str] = []

        n_len = len(name.strip())
        if n_len >= 28:
            score += 25
        elif n_len >= 14:
            score += 15
            tips.append("Alonga o título para 28–75 caracteres com modelo, medidas (mm) e aplicação.")
        else:
            tips.append("Título demasiado curto. Inclui palavras-chave específicas pesquisadas pelos compradores.")

        d_len = len(description.strip())
        if d_len >= 300:
            score += 20
        elif d_len >= 120:
            score += 12
            tips.append("Expande a descrição (> 300 caracteres) com tolerâncias, montagem e vantagens.")
        else:
            tips.append("Descrição muito curta. O motor de busca favorece descrições técnicas completas.")

        desc_lower = description.lower()
        if any(k in desc_lower for k in ("infill", "layer", "support", "petg", "pla", "abs", "asa", "nozzle", "parede", "camada")):
            score += 15
        else:
            tips.append("Adiciona definições de impressão (material, altura de camada, infill e suportes).")

        clean_tags = [t.strip().lower() for t in tags if t.strip()]
        if len(clean_tags) >= 15:
            score += 30
        elif len(clean_tags) >= 8:
            score += 20
            tips.append(f"Tens {len(clean_tags)}/20 tags. Preencher até 20 tags aumenta as impressões orgânicas.")
        else:
            score += 6
            tips.append(f"Apenas {len(clean_tags)}/20 tags! Usa todas as 20 tags disponíveis.")

        if price == 0 or (round(price * 100) % 50 == 0 or str(f"{price:.2f}").endswith(("9", "5", "0"))):
            score += 10
        else:
            score += 6

        suggested_tags = cls.build_20_tags(name, description, clean_tags, max_tags=20)
        missing_tags = [t for t in suggested_tags if t not in clean_tags]

        return {
            "score": min(100, score),
            "tips": tips,
            "tag_count": len(clean_tags),
            "suggested_tags": suggested_tags,
            "missing_tags": missing_tags[:10],
        }

    @classmethod
    def auto_optimize_listing(
        cls,
        name: str,
        description: str,
        tags: List[str],
        add_imperial: bool = True,
        ensure_print_block: bool = True,
        extra_tags: Optional[List[str]] = None,
    ) -> Tuple[str, str, List[str]]:
        new_name = name.strip()
        new_desc = description.strip()

        if add_imperial:
            new_name = cls.enrich_metric_units(new_name)
            new_desc = cls.enrich_metric_units(new_desc)

        if ensure_print_block and not any(
            k in new_desc.lower() for k in ("print settings", "infill", "layer height", "petg", "pla", "abs", "asa")
        ):
            new_desc = (
                f"{new_desc}\n\n"
                "### Recommended 3D Print Settings\n"
                "- Material: PETG, ASA or ABS for mechanical/heat durability (PLA+ for indoor use)\n"
                "- Layer Height: 0.20 mm | Nozzle: 0.40 mm\n"
                "- Infill: 25%–40% Gyroid | Walls: 3–4 perimeters"
            ).strip()

        new_tags = cls.build_20_tags(new_name, new_desc, tags, extra_priority_tags=extra_tags, max_tags=20)
        return new_name, new_desc, new_tags


def extract_ngrams(titles: List[str], n: int = 2, top_k: int = 10) -> List[Tuple[str, int]]:
    stopwords = {"the", "and", "for", "with", "stl", "file", "print", "3d", "model", "part", "from"}
    grams: List[str] = []
    for title in titles:
        tokens = [
            w for w in re.findall(r"[a-z0-9]{2,}", title.lower())
            if w not in stopwords
        ]
        if len(tokens) >= n:
            for i in range(len(tokens) - n + 1):
                grams.append(" ".join(tokens[i:i + n]))
    return Counter(grams).most_common(top_k)


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
        schema = self.get_schema_info()
        queries = schema["queries"]

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
# HELPERS DE MARKETING & KIT SOCIAL TELEGRAM
# ==============================================================================
def build_social_promo_kit(
    name: str,
    url: str,
    price: float,
    currency: str,
    is_promo: bool = False,
    discount_pct: int = 0,
) -> str:
    price_tag = (
        f"🔥 {discount_pct}% OFF right now!"
        if is_promo
        else ("🆓 FREE STL download!" if price == 0 else f"Available for {price:.2f} {currency}")
    )
    reddit_post = (
        f"[3D Print] {name} — {price_tag}\n"
        f"Designed for clean, reliable printing. Full print settings & STL files here: {url}"
    )
    return (
        f"📣 <b>Kit de Divulgação Pronto a Colar:</b>\n"
        f"<code>{reddit_post}</code>"
    )


def build_post_sale_reply(buyer_nick: str, model_name: str) -> str:
    clean_buyer = buyer_nick if buyer_nick and buyer_nick != "Anónimo" else "maker"
    msg = (
        f"Hi {clean_buyer}! Thank you so much for downloading '{model_name}'. "
        f"If you print it, I'd love to see a Make photo on Cults3D! Feel free to reach out if you need any print tips."
    )
    return (
        f"💬 <b>Mensagem Pós-Venda (copiar e enviar no perfil):</b>\n"
        f"<code>{msg}</code>"
    )


# ==============================================================================
# MOTOR DO BOT AUTÓNOMO (ANTI-SLEEP + GROWTH ENGINE + PERSISTÊNCIA)
# ==============================================================================
class CultsBotEngine:
    DEFAULT_CONFIG: Dict[str, Any] = {
        "interval_minutes": 15,
        "dry_run": True,
        "max_actions_per_cycle": 5,
        "app_url": "",
        "anti_sleep_enabled": True,
        "tg_token": "",
        "tg_chat_id": "",
        "tg_remote_commands": True,
        "discord_webhook": "",
        "rule_sales_alert": True,
        "rule_milestone_alert": True,
        "rule_post_sale_script": True,
        "rule_social_kit": True,
        "rule_spike_alert": True,
        "spike_views_threshold": 35,
        "rule_auto_promo": False,
        "promo_min_views": 120,
        "promo_discount_pct": 20,
        "promo_days": 3,
        "promo_cooldown_days": 14,
        "rule_stagnant_revival": False,
        "stagnant_days": 30,
        "stagnant_discount_pct": 25,
        "stagnant_promo_days": 3,
        "rule_dynamic_price": False,
        "price_step_sales": 10,
        "price_bump_pct": 15.0,
        "price_max_cap": 15.0,
        "rule_weekend_flash": False,
        "weekend_day": 4,
        "weekend_hour": 18,
        "weekend_discount_pct": 20,
        "weekend_days": 3,
        "rule_dynamic_showcase": False,
        "showcase_top_n": 3,
        "showcase_title": "Top Popular Designs by FSDesigns:",
        "rule_seo_optimizer": False,
        "seo_footer_marker": "--- FSDesigns ---",
        "seo_footer_text": "\n\n--- FSDesigns ---\nDesigned for reliable 3D printing (PETG / ABS / ASA recommended for automotive parts). Follow our profile for new functional designs!",
        "seo_required_tags": "stl, 3d print",
        "rule_queue_publisher": True,
        "rule_market_watch": False,
        "watch_keywords": "",
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
            "faq": "- Will it warp in summer heat? Print in ASA, ABS or PETG (avoid PLA inside vehicles).\n- Need extra hardware? Designed for direct OEM-style fitment.",
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
            "faq": "- Does it need supports? Zero supports required.\n- Can I scale it down? Tested down to 80% scale with 0.4 mm nozzle.",
            "license": "Personal use only. Do not redistribute or sell STL files.",
        },
        "Organização & Oficina (PLA / PETG)": {
            "category_hint": "tool",
            "price": 2.90,
            "locale": "EN",
            "tags": "organizer, holder, mount, workshop, tool, functional, storage, stl",
            "overview": "Heavy-duty organizer and mount engineered for clean workspace storage and fast printing.",
            "print_settings": "- Material: PETG or PLA+\n- Layer height: 0.24 mm (0.4 mm or 0.6 mm nozzle)\n- Infill: 20% Grid/Gyroid\n- Perimeters: 3 walls\n- Supports: None",
            "assembly": "Mounts using standard countersunk wood/wall screws.",
            "faq": "- Fits standard screws? Yes, 3.5 mm to 4.5 mm countersunk screws.",
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
        self.early_bird_rules: Dict[str, Dict[str, Any]] = {}
        self.ab_tests: Dict[str, Dict[str, Any]] = {}
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
        self.last_keepalive_ts: Optional[str] = None
        self.keepalive_status: str = "A aguardar URL"
        self.publish_queue: List[Dict[str, Any]] = []
        self.action_history: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, str]] = []
        self.last_run: Optional[str] = None

        self._load_state()
        self._apply_env_overrides()

    def _apply_env_overrides(self):
        with self._lock:
            if get_secret_val("TG_TOKEN"):
                self.config["tg_token"] = get_secret_val("TG_TOKEN")
            if get_secret_val("TG_CHAT_ID"):
                self.config["tg_chat_id"] = get_secret_val("TG_CHAT_ID")
            if get_secret_val("DISCORD_WEBHOOK"):
                self.config["discord_webhook"] = get_secret_val("DISCORD_WEBHOOK")
            if get_secret_val("APP_URL"):
                self.config["app_url"] = get_secret_val("APP_URL")
            if get_secret_val("WATCH_KEYWORDS"):
                self.config["watch_keywords"] = get_secret_val("WATCH_KEYWORDS")
            if get_secret_val("BOT_DRY_RUN"):
                self.config["dry_run"] = get_secret_val("BOT_DRY_RUN").lower() in ("1", "true", "sim", "yes")
            if get_secret_val("RULE_AUTO_PROMO"):
                self.config["rule_auto_promo"] = get_secret_val("RULE_AUTO_PROMO").lower() in ("1", "true", "sim")
            if get_secret_val("RULE_DYNAMIC_SHOWCASE"):
                self.config["rule_dynamic_showcase"] = get_secret_val("RULE_DYNAMIC_SHOWCASE").lower() in ("1", "true", "sim")
            if get_secret_val("RULE_DYNAMIC_PRICE"):
                self.config["rule_dynamic_price"] = get_secret_val("RULE_DYNAMIC_PRICE").lower() in ("1", "true", "sim")

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
                self.early_bird_rules = dict(data.get("early_bird_rules") or {})
                self.ab_tests = dict(data.get("ab_tests") or {})
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
                "config": {
                    k: v for k, v in self.config.items()
                    if k not in ("tg_token", "tg_chat_id", "discord_webhook")
                },
                "presets": self.presets,
                "seen_sale_ids": self.seen_sale_ids[-2500:],
                "sales_seeded": self.sales_seeded,
                "celebrated_milestones": self.celebrated_milestones[-1000:],
                "promo_cooldowns": self.promo_cooldowns,
                "price_tiers_applied": self.price_tiers_applied,
                "early_bird_rules": self.early_bird_rules,
                "ab_tests": self.ab_tests,
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
                clean_text = text.replace("<b>", "**").replace("</b>", "**").replace("<code>", "`").replace("</code>", "`")
                requests.post(discord_url, json={"content": clean_text}, timeout=10)
            except Exception as exc:
                self.log("ERRO", f"Falha Discord: {exc}")

    def ping_keep_alive(self):
        with self._lock:
            if not self.config.get("anti_sleep_enabled", True):
                self.keepalive_status = "Desativado"
                return
            raw_url = str(self.config.get("app_url", "")).strip()

        if not raw_url:
            with self._lock:
                self.keepalive_status = "Falta APP_URL"
            return

        sep = "&" if "?" in raw_url else "?"
        target_url = f"{raw_url.rstrip('/')}{sep}health=1"
        try:
            resp = requests.get(
                target_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Cults3DKeepAlive/1.0)"},
                timeout=12,
            )
            with self._lock:
                self.last_keepalive_ts = datetime.now().strftime("%H:%M:%S")
                self.keepalive_status = f"OK ({resp.status_code}) às {self.last_keepalive_ts}"
        except Exception as exc:
            with self._lock:
                self.keepalive_status = f"Falha: {exc}"

    def poll_telegram_commands(self):
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
                        f"🤖 <b>Estado Cults3D Studio Pro v5</b>\n"
                        f"Estado: {state_lbl} ({mode_lbl})\n"
                        f"Anti-Sleep: {self.keepalive_status}\n"
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
                elif cmd in ("/vendas", "/top", "/kit") and user and key:
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
                    elif cmd == "/kit":
                        creations = c_api.get_all_my_creations(currency=curr)
                        if creations:
                            best = max(creations, key=lambda x: int(x.get("viewsCount") or 0))
                            p_val = ((best.get("price") or {}).get("cents") or 0) / 100.0
                            kit = build_social_promo_kit(best.get("name", ""), best.get("url", ""), p_val, curr)
                            self.send_notification(kit)

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
        self.ping_keep_alive()
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
                if step > 0 and step % 240 == 0:
                    self.ping_keep_alive()
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

            sales = client.get_all_my_sales(currency=curr)
            sales_count_map: Dict[str, int] = {}
            sales_rev_map: Dict[str, float] = {}
            last_sale_date_map: Dict[str, str] = {}
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_sales_count = 0
            today_revenue = 0.0

            for s in sales:
                cr_id = str((s.get("creation") or {}).get("id") or "")
                created_at = str(s.get("createdAt") or "")
                inc_cents = (s.get("income") or {}).get("cents") or 0
                inc_eur = inc_cents / 100.0
                if cr_id:
                    sales_count_map[cr_id] = sales_count_map.get(cr_id, 0) + 1
                    sales_rev_map[cr_id] = round(sales_rev_map.get(cr_id, 0.0) + inc_eur, 2)
                    if created_at > last_sale_date_map.get(cr_id, ""):
                        last_sale_date_map[cr_id] = created_at
                if created_at.startswith(today_str):
                    today_sales_count += 1
                    today_revenue += inc_eur

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
                        if cfg.get("rule_post_sale_script", True):
                            msg += "\n\n" + build_post_sale_reply(buyer, m_name)

                        self.log("VENDA", f"Nova venda: '{m_name}' (+{inc_val:.2f} {curr}) por {buyer}.")
                        self.send_notification(msg)

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

            creations = client.get_all_my_creations(currency=curr)
            self.seed_baselines_if_needed(creations)
            spike_alerts: List[str] = []

            showcase_block = ""
            top_showcase_ids = set()
            if cfg.get("rule_dynamic_showcase") and len(creations) > 1:
                top_n = max(1, min(5, int(cfg.get("showcase_top_n", 3))))
                ranked = sorted(
                    [x for x in creations if x.get("url")],
                    key=lambda x: (sales_count_map.get(str(x.get("id")), 0) * 50) + int(x.get("viewsCount") or 0),
                    reverse=True,
                )[:top_n]
                if ranked:
                    top_showcase_ids = {str(x["id"]) for x in ranked}
                    sc_title = str(cfg.get("showcase_title") or "Top Popular Designs:").strip()
                    lines = ["=== TOP DESIGNS ===", sc_title]
                    for r_item in ranked:
                        lines.append(f"• {r_item.get('name')}: {r_item.get('url')}")
                    lines.append("=== END TOP DESIGNS ===")
                    showcase_block = "\n".join(lines)

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
                curl = c.get("url") or ""
                cdesc = c.get("description") or ""
                ctags = list(c.get("tags") or [])
                cviews = int(c.get("viewsCount") or 0)
                cdowns = int(c.get("downloadsCount") or 0)
                cprice = round(((c.get("price") or {}).get("cents") or 0) / 100.0, 2)
                csales = sales_count_map.get(cid, 0)
                crev = sales_rev_map.get(cid, 0.0)

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

                # Early-Bird
                eb_rule = self.early_bird_rules.get(cid)
                if eb_rule and not eb_rule.get("completed") and actions_done < max_actions:
                    target_s = int(eb_rule.get("target_sales", 3))
                    launch_p = round(float(eb_rule.get("launch_price", cprice)), 2)
                    reg_p = round(float(eb_rule.get("regular_price", cprice)), 2)

                    if csales >= target_s:
                        if dry:
                            self.log("SIMULAÇÃO", f"[Early-Bird] '{cname}' atingiu {csales}/{target_s} vendas -> subiria para {reg_p:.2f} {curr}.")
                        else:
                            try:
                                client.update_creation(creation_id=cid, price=reg_p, currency=curr)
                                with self._lock:
                                    eb_rule["completed"] = True
                                actions_done += 1
                                self.log("EARLY-BIRD", f"'{cname}' atingiu {csales} vendas! Preço subiu para {reg_p:.2f} {curr}.")
                                self.record_action("EARLY_BIRD_FIM", cid, cname, f"{launch_p:.2f} -> {reg_p:.2f} {curr}", before_state={"price": launch_p}, after_state={"price": reg_p}, reversible=True)
                                self.send_notification(f"🚀 <b>Early-Bird Concluído!</b>\nModelo: {cname}\nAtingiu {csales} vendas e subiu para <b>{reg_p:.2f} {curr}</b>!")
                            except Exception as exc:
                                self.log("ERRO", f"Falha Early-Bird em '{cname}': {exc}")
                    elif abs(cprice - launch_p) > 0.01 and not dry:
                        try:
                            client.update_creation(creation_id=cid, price=launch_p, currency=curr)
                            actions_done += 1
                        except Exception:
                            pass

                # Teste A/B por RPM
                ab = self.ab_tests.get(cid)
                if ab and ab.get("phase") in ("A", "B") and actions_done < max_actions:
                    try:
                        p_start = datetime.fromisoformat(ab["phase_start"].replace("Z", "+00:00"))
                        elapsed_days = (datetime.now(timezone.utc) - p_start).total_seconds() / 86400.0
                        phase_days = float(ab.get("days_per_phase", 7))

                        if elapsed_days >= phase_days:
                            d_views = max(1, cviews - int(ab.get("start_views", cviews)))
                            d_rev = max(0.0, crev - float(ab.get("start_rev", crev)))
                            rpm_val = round((d_rev / d_views) * 1000.0, 2)

                            if ab["phase"] == "A":
                                price_b = round(float(ab["price_b"]), 2)
                                if not dry:
                                    client.update_creation(creation_id=cid, price=price_b, currency=curr)
                                    with self._lock:
                                        ab["rpm_a"] = rpm_val
                                        ab["phase"] = "B"
                                        ab["phase_start"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                        ab["start_views"] = cviews
                                        ab["start_rev"] = crev
                                    actions_done += 1
                                    self.log("TESTE A/B", f"'{cname}': Fase A concluída (RPM={rpm_val}). A iniciar Fase B ({price_b:.2f} {curr}).")
                                    self.send_notification(f"🧪 <b>Teste A/B — Fase B Iniciada</b>\nModelo: {cname}\nRPM Preço A: {rpm_val} {curr}/1k\nNovo preço em teste: <b>{price_b:.2f} {curr}</b>")
                            elif ab["phase"] == "B":
                                rpm_a = float(ab.get("rpm_a", 0.0))
                                winner = round(float(ab["price_a"] if rpm_a >= rpm_val else ab["price_b"]), 2)
                                if not dry:
                                    client.update_creation(creation_id=cid, price=winner, currency=curr)
                                    with self._lock:
                                        ab["rpm_b"] = rpm_val
                                        ab["phase"] = "DONE"
                                        ab["winner_price"] = winner
                                    actions_done += 1
                                    self.log("TESTE A/B", f"'{cname}' concluído! Vencedor: {winner:.2f} {curr} (RPM A={rpm_a} vs B={rpm_val}).")
                                    self.record_action("TESTE_AB_VENCEDOR", cid, cname, f"Preço vencedor: {winner:.2f} {curr}", before_state={"price": cprice}, after_state={"price": winner}, reversible=True)
                                    self.send_notification(
                                        f"🏆 <b>Teste A/B Concluído!</b>\nModelo: {cname}\n"
                                        f"RPM A ({ab['price_a']:.2f}): {rpm_a} | RPM B ({ab['price_b']:.2f}): {rpm_val}\n"
                                        f"Preço Vencedor Aplicado: <b>{winner:.2f} {curr}</b>"
                                    )
                    except Exception as exc:
                        self.log("ERRO", f"Falha Teste A/B em '{cname}': {exc}")

                # Auto-Promo (0 Vendas)
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
                            promo_msg = f"🔥 <b>Auto-Promo Ativada</b>\nModelo: {cname}\nDesconto: {pct}% ({days} dias)"
                            if cfg.get("rule_social_kit", True) and curl:
                                promo_msg += "\n\n" + build_social_promo_kit(cname, curl, cprice, curr, is_promo=True, discount_pct=pct)
                            self.send_notification(promo_msg)
                        except Exception as exc:
                            with self._lock:
                                self.promo_cooldowns[cid] = (
                                    datetime.now(timezone.utc) + timedelta(days=days)
                                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                            self.log("AVISO", f"Auto-Promo ignorada em '{cname}': {exc}")

                # Reativação Estagnados
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
                                    self.log("SIMULAÇÃO", f"[Reativação] '{cname}' sem vendas há {days_since}d -> desconto {s_pct}%.")
                                else:
                                    client.create_discount(cid, s_pct, end_iso)
                                    with self._lock:
                                        self.promo_cooldowns[cid] = cd_until
                                    actions_done += 1
                                    self.log("REATIVAÇÃO", f"Desconto {s_pct}% em '{cname}' (parado há {days_since}d).")
                                    self.record_action("REATIVAÇÃO", cid, cname, f"Desconto {s_pct}% após {days_since}d sem vendas")
                        except Exception:
                            pass

                # Preço Dinâmico
                if (
                    actions_done < max_actions
                    and cfg.get("rule_dynamic_price")
                    and cprice > 0
                    and cid not in self.early_bird_rules
                    and cid not in self.ab_tests
                ):
                    step_sales = max(1, int(cfg.get("price_step_sales", 10)))
                    earned_tiers = csales // step_sales
                    applied_tiers = int(self.price_tiers_applied.get(cid, 0))
                    max_cap = float(cfg.get("price_max_cap", 15.0))
                    bump_pct = float(cfg.get("price_bump_pct", 15.0))

                    if earned_tiers > applied_tiers and cprice < max_cap:
                        new_price = min(round(max_cap, 2), round(cprice * (1.0 + bump_pct / 100.0), 2))
                        if new_price > cprice:
                            if dry:
                                self.log("SIMULAÇÃO", f"[Preço Dinâmico] '{cname}': {cprice:.2f} -> {new_price:.2f} {curr} ({csales} vendas).")
                            else:
                                try:
                                    client.update_creation(creation_id=cid, price=new_price, currency=curr)
                                    with self._lock:
                                        self.price_tiers_applied[cid] = earned_tiers
                                    actions_done += 1
                                    self.log("PREÇO", f"'{cname}': {cprice:.2f} -> {new_price:.2f} {curr}.")
                                    self.record_action("PREÇO_DINÂMICO", cid, cname, f"{cprice:.2f} -> {new_price:.2f} {curr}", before_state={"price": cprice}, after_state={"price": new_price}, reversible=True)
                                    self.send_notification(f"📈 <b>Preço Atualizado</b>\nModelo: {cname}\n{cprice:.2f} ➔ <b>{new_price:.2f} {curr}</b>")
                                except Exception as exc:
                                    self.log("ERRO", f"Falha preço em '{cname}': {exc}")

                # Flash Fim de Semana
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

                # Vitrine Dinâmica & SEO
                new_desc_candidate = cdesc
                desc_modified = False

                if showcase_block and cid not in top_showcase_ids:
                    cleaned_desc = re.sub(
                        r"\n*=== TOP DESIGNS ===.*?=== END TOP DESIGNS ===\n*",
                        "\n\n",
                        new_desc_candidate,
                        flags=re.DOTALL,
                    ).strip()
                    composed = f"{cleaned_desc}\n\n{showcase_block}".strip()
                    if composed != new_desc_candidate.strip():
                        new_desc_candidate = composed
                        desc_modified = True

                new_tags_candidate = ctags
                tags_modified = False
                if cfg.get("rule_seo_optimizer") and cid not in self.seo_processed_ids:
                    marker = str(cfg.get("seo_footer_marker", "")).strip()
                    footer = str(cfg.get("seo_footer_text", ""))
                    req_tags = [
                        t.strip().lower()
                        for t in str(cfg.get("seo_required_tags", "")).split(",")
                        if t.strip()
                    ]
                    if marker and footer and marker not in new_desc_candidate:
                        new_desc_candidate = (new_desc_candidate.rstrip() + footer).strip()
                        desc_modified = True

                    enriched_tags = SeoOptimizerEngine.build_20_tags(
                        cname, new_desc_candidate, ctags, extra_priority_tags=req_tags, max_tags=20
                    )
                    if enriched_tags != ctags:
                        new_tags_candidate = enriched_tags
                        tags_modified = True

                    if not desc_modified and not tags_modified:
                        with self._lock:
                            self.seo_processed_ids.append(cid)

                if (desc_modified or tags_modified) and actions_done < max_actions:
                    if dry:
                        self.log("SIMULAÇÃO", f"[Vitrine/SEO] Atualizaria descrição/tags em '{cname}'.")
                    else:
                        try:
                            client.update_creation(
                                creation_id=cid,
                                description=new_desc_candidate if desc_modified else None,
                                tags=new_tags_candidate if tags_modified else None,
                            )
                            if cfg.get("rule_seo_optimizer") and cid not in self.seo_processed_ids:
                                with self._lock:
                                    self.seo_processed_ids.append(cid)
                            actions_done += 1
                            self.log("VITRINE/SEO", f"Vitrine/SEO atualizado em '{cname}'.")
                            self.record_action(
                                "VITRINE_SEO",
                                cid,
                                cname,
                                f"Descrição={desc_modified}, Tags={tags_modified}",
                                before_state={"description": cdesc, "tags": ctags},
                                after_state={"description": new_desc_candidate, "tags": new_tags_candidate},
                                reversible=True,
                            )
                        except Exception as exc:
                            self.log("ERRO", f"Falha Vitrine/SEO em '{cname}': {exc}")

            if trigger_weekend_flash and not dry:
                with self._lock:
                    self.last_weekend_flash_week = current_week_key
                self.send_notification(
                    f"⚡ <b>Promoção Relâmpago de Fim de Semana Ativa!</b>\nDesconto de {cfg.get('weekend_discount_pct')}% aplicado aos modelos elegíveis."
                )

            if spike_alerts:
                self.log("TRÁFEGO", f"{len(spike_alerts)} modelo(s) com pico de tráfego!")
                self.send_notification("🚀 <b>Pico de Tráfego Detetado!</b>\n" + "\n".join(spike_alerts[:6]))

            # Fila de Publicação Agendada
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
                                pub_msg = f"🚀 <b>Novo Modelo Publicado!</b>\n📦 {item['name']}\n🔗 {pub_url}"
                                if cfg.get("rule_social_kit", True) and pub_url:
                                    pub_msg += "\n\n" + build_social_promo_kit(item["name"], pub_url, float(item.get("price", 0.0)), curr)
                                self.send_notification(pub_msg)
                            except Exception as exc:
                                with self._lock:
                                    item["status"] = f"Erro: {exc}"
                                self.log("ERRO", f"Falha ao publicar '{item['name']}': {exc}")
                        break

            # Radar de Nichos
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
                            found_new_items.append(f"• <b>{m_title}</b> ({m_price:.2f} {curr}) por {m_creator}\n  👉 {m_url}")

                with self._lock:
                    self.seen_market_ids = list(seen_m)
                    if not self.market_seeded:
                        self.market_seeded = True
                        self.log("RADAR", f"Radar inicializado com {len(seen_m)} peças monitorizadas.")

                if found_new_items:
                    self.log("RADAR", f"{len(found_new_items)} nova(s) peça(s) no radar.")
                    self.send_notification(
                        "🕵️ <b>Novidades nos Nichos (Oportunidade de 1.º Comentário):</b>\n"
                        + "\n".join(found_new_items[:5])
                    )

            # Relatórios Diário & Semanal
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
def get_bot_engine(version: str = "studio_pro_v5") -> CultsBotEngine:
    return CultsBotEngine()


bot = get_bot_engine("studio_pro_v5")
if not hasattr(bot, "ping_keep_alive"):
    try:
        bot.stop()
    except Exception:
        pass
    get_bot_engine.clear()
    bot = get_bot_engine("studio_pro_v5")

# Arranque automático via Secrets (Cloud 24/7)
_srv_user = get_secret_val("CULTS_USERNAME")
_srv_key = get_secret_val("CULTS_API_KEY")
if _srv_user and _srv_key:
    _srv_curr = get_secret_val("CULTS_CURRENCY", "EUR") or "EUR"
    bot.set_credentials(_srv_user, _srv_key, _srv_curr)
    if not bot.running:
        bot.start()

# Resposta ultra-rápida para Pings Anti-Sleep (?health=1)
if st.query_params.get("health") == "1":
    st.write("OK")
    st.stop()


# ==============================================================================
# GESTÃO DE ESTADO DA SESSÃO
# ==============================================================================
for key, default in [
    ("username", get_secret_val("CULTS_USERNAME", "")),
    ("api_key", get_secret_val("CULTS_API_KEY", "")),
    ("currency", get_secret_val("CULTS_CURRENCY", "EUR") or "EUR"),
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
        st.title("🧊 Cults3D Studio Pro v5")
        st.write(
            "Plataforma completa de Business Intelligence, Upload Drag & Drop, Estúdio SEO em Massa e Bot Autónomo 24/7."
        )

        with st.form("login_form"):
            in_user = st.text_input(
                "Username Cults3D",
                value=st.session_state.username,
                placeholder="Ex: FSDesigns",
            )
            in_key = st.text_input(
                "API Key",
                value=st.session_state.api_key,
                type="password",
                placeholder="Cola aqui a tua API Key",
            )
            in_curr = st.selectbox("Moeda principal", ["EUR", "USD", "GBP"], index=0)
            submit_login = st.form_submit_button(
                "🔌 Ligar e Sincronizar Conta",
                type="primary",
                use_container_width=True,
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
        st.info(
            f"🤖 Bot: **ATIVO** ({'Simulação' if bot.config['dry_run'] else 'Real'})\n\n"
            f"🛡️ Anti-Sleep: `{bot.keepalive_status}`"
        )
    else:
        st.caption("🤖 Bot: Parado")

    new_curr = st.selectbox("Moeda", ["EUR", "USD", "GBP"], index=["EUR", "USD", "GBP"].index(currency))
    if new_curr != currency:
        st.session_state.currency = new_curr
        bot.set_credentials(st.session_state.username, st.session_state.api_key, new_curr)
        with st.spinner("A atualizar moeda..."):
            try:
                sync_account(client, new_curr)
                st.rerun()
            except Exception as e:
                st.error(str(e))

    if st.button("🔄 Sincronizar Tudo", type="primary", use_container_width=True):
        with st.spinner("A sincronizar..."):
            try:
                sync_account(client, st.session_state.currency)
                st.toast("Dados atualizados!")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    if st.button("🚪 Trocar Conta / API Key", use_container_width=True):
        bot.stop()
        st.session_state.connected = False
        st.session_state.api_key = ""
        st.session_state.creations = []
        st.session_state.sales = []
        st.rerun()


# ==============================================================================
# PREPARAÇÃO DE DATAFRAMES & MÉTRICAS SEO
# ==============================================================================
def build_dataframes():
    creations_rows = []
    for c in st.session_state.creations:
        price_obj = c.get("price") or {}
        tags_raw = c.get("tags") or []
        name_val = c.get("name") or ""
        desc_val = c.get("description") or ""
        price_val = round((price_obj.get("cents") or 0) / 100.0, 2)
        seo_info = SeoOptimizerEngine.analyze_listing(name_val, desc_val, tags_raw, price_val)

        creations_rows.append({
            "id": str(c.get("id")),
            "nome": name_val,
            "descricao": desc_val,
            "preco": price_val,
            "views": int(c.get("viewsCount") or 0),
            "likes": int(c.get("likesCount") or 0),
            "downloads": int(c.get("downloadsCount") or 0),
            "publicado_em": c.get("publishedAt"),
            "tags_list": tags_raw,
            "tags": ", ".join(tags_raw),
            "num_tags": len(tags_raw),
            "seo_score": seo_info["score"],
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

tab_dash, tab_publish, tab_bulk, tab_bot, tab_market, tab_console = st.tabs([
    "📊 Analytics & BI",
    "🚀 Publicar (Drag & Drop)",
    "⚡ Gestão & SEO em Massa",
    "🤖 Bot & Growth Engine",
    "🔍 Radar N-Gram",
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
        conv_rate = (total_vendas / total_views * 100.0) if total_views > 0 else 0.0
        avg_seo = int(round(df_creations["seo_score"].mean()))

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric(f"Receita Líquida ({currency})", f"{total_receita:,.2f}")
        k2.metric("Vendas Totais", f"{total_vendas}")
        k3.metric(f"Ticket Médio ({currency})", f"{ticket_medio:,.2f}")
        k4.metric("Visualizações", f"{total_views:,}")
        k5.metric("Conversão Global", f"{conv_rate:.2f}%")
        k6.metric("SEO Médio Loja", f"{avg_seo}/100")

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

        perf_df["conversao_%"] = (perf_df["vendas"] / perf_df["views"].replace(0, pd.NA) * 100.0).fillna(0.0).round(2)
        perf_df["likes_ratio_%"] = (perf_df["likes"] / perf_df["views"].replace(0, pd.NA) * 100.0).fillna(0.0).round(1)

        med_views = max(50.0, float(perf_df["views"].median()))
        med_conv = max(0.5, float(perf_df[perf_df["vendas"] > 0]["conversao_%"].median()) if (perf_df["vendas"] > 0).any() else 1.0)

        def classify_tier(row) -> str:
            if row["preco"] == 0:
                return "🎁 Gratuito (Íman)"
            if row["views"] >= med_views and row["conversao_%"] >= med_conv and row["vendas"] > 0:
                return "🏆 Estrela (Best-Seller)"
            if row["views"] < med_views and row["vendas"] > 0:
                return "💎 Joia Escondida"
            if row["views"] >= med_views and row["vendas"] == 0:
                return "👀 Muito Visto / 0 Vendas"
            return "💤 Em Crescimento"

        perf_df["estado_bcg"] = perf_df.apply(classify_tier, axis=1)

        st.divider()

        if not df_sales.empty:
            dt_series = pd.to_datetime(df_sales["data"], utc=True, errors="coerce")
            df_sales["dt_utc"] = dt_series
            valid_sales = df_sales.dropna(subset=["dt_utc"]).copy()

            c_left, c_right = st.columns([2, 1])
            with c_left:
                hdr1, hdr2 = st.columns([2, 1])
                hdr1.subheader("Evolução de Receita")
                agg_mode = hdr2.selectbox("Agrupamento", ["Dia", "Semana", "Mês", "Acumulado"], label_visibility="collapsed")
                if not valid_sales.empty:
                    if agg_mode == "Dia":
                        st.bar_chart(valid_sales.groupby(valid_sales["dt_utc"].dt.date)["receita_liquida"].sum())
                    elif agg_mode == "Semana":
                        st.bar_chart(valid_sales.groupby(valid_sales["dt_utc"].dt.to_period("W").astype(str))["receita_liquida"].sum())
                    elif agg_mode == "Mês":
                        st.bar_chart(valid_sales.groupby(valid_sales["dt_utc"].dt.to_period("M").astype(str))["receita_liquida"].sum())
                    else:
                        st.area_chart(valid_sales.groupby(valid_sales["dt_utc"].dt.date)["receita_liquida"].sum().cumsum())

            with c_right:
                st.subheader("Melhores Dias de Venda")
                if not valid_sales.empty:
                    dias_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
                    valid_sales["dia_sem"] = valid_sales["dt_utc"].dt.dayofweek.map(lambda d: dias_pt[d])
                    st.bar_chart(valid_sales.groupby("dia_sem")["sale_id"].count().reindex(dias_pt, fill_value=0))

        st.divider()
        st.subheader("Matriz de Performance & SEO do Catálogo")
        filter_tier = st.multiselect("Filtrar por Diagnóstico", options=sorted(perf_df["estado_bcg"].unique().tolist()))
        view_df = perf_df[perf_df["estado_bcg"].isin(filter_tier)] if filter_tier else perf_df

        st.dataframe(
            view_df[["nome", "estado_bcg", "seo_score", "num_tags", "preco", "views", "likes", "vendas", "receita", "conversao_%", "url"]].sort_values(by=["receita", "views"], ascending=False),
            use_container_width=True,
            hide_index=True,
            column_config={
                "nome": "Modelo",
                "estado_bcg": "Diagnóstico",
                "seo_score": st.column_config.ProgressColumn("SEO", min_value=0, max_value=100, format="%d/100"),
                "num_tags": "Tags",
                "url": st.column_config.LinkColumn("Página", display_text="Abrir"),
                "preco": st.column_config.NumberColumn(f"Preço ({currency})", format="%.2f"),
                "receita": st.column_config.NumberColumn(f"Receita ({currency})", format="%.2f"),
                "conversao_%": st.column_config.NumberColumn("Conv. (%)", format="%.2f%%"),
            },
        )

        with st.expander("🔬 Inspetor & Otimizador Individual (1 Clique)"):
            model_names_map = {row["nome"]: row for _, row in perf_df.iterrows()}
            chosen_name = st.selectbox("Escolhe um modelo", list(model_names_map.keys()))
            m_row = model_names_map[chosen_name]
            seo_eval = SeoOptimizerEngine.analyze_listing(m_row["nome"], m_row["descricao"], m_row["tags_list"], m_row["preco"])

            ic1, ic2, ic3, ic4 = st.columns(4)
            ic1.metric("SEO Score", f"{seo_eval['score']}/100")
            ic2.metric("Tags Atuais", f"{m_row['num_tags']}/20")
            ic3.metric("Diagnóstico", m_row["estado_bcg"].split(" (")[0])
            ic4.metric("Receita Gerada", f"{m_row['receita']:.2f} {currency}")

            if seo_eval["tips"]:
                st.warning("💡 **Oportunidades SEO:** " + " | ".join(seo_eval["tips"]))

            if st.button("✨ Auto-Preencher SEO & 20 Tags Otimizadas nos Campos Abaixo", key="btn_autofill_single"):
                opt_n, opt_d, opt_t = SeoOptimizerEngine.auto_optimize_listing(
                    m_row["nome"], m_row["descricao"], m_row["tags_list"]
                )
                st.session_state[f"qe_n_{m_row['id']}"] = opt_n
                st.session_state[f"qe_d_{m_row['id']}"] = opt_d
                st.session_state[f"qe_t_{m_row['id']}"] = ", ".join(opt_t)
                st.rerun()

            with st.form("quick_edit_single_form"):
                qe_name = st.text_input("Título", value=st.session_state.get(f"qe_n_{m_row['id']}", m_row["nome"]))
                qe_price = st.number_input(f"Preço ({currency})", min_value=0.0, step=0.5, value=float(m_row["preco"]))
                qe_tags = st.text_input("Tags (até 20, separadas por vírgula)", value=st.session_state.get(f"qe_t_{m_row['id']}", m_row["tags"]))
                qe_desc = st.text_area("Descrição", value=st.session_state.get(f"qe_d_{m_row['id']}", m_row["descricao"]), height=140)
                if st.form_submit_button("💾 Guardar Alterações no Cults3D", type="primary"):
                    try:
                        final_tags = [t.strip() for t in qe_tags.split(",") if t.strip()][:20]
                        client.update_creation(
                            creation_id=m_row["id"],
                            name=qe_name.strip(),
                            description=qe_desc.strip(),
                            price=qe_price,
                            currency=currency,
                            tags=final_tags,
                        )
                        bot.record_action(
                            "EDIÇÃO_INDIVIDUAL",
                            m_row["id"],
                            m_row["nome"],
                            f"SEO & Preço ({qe_price:.2f} {currency})",
                            before_state={"name": m_row["nome"], "description": m_row["descricao"], "price": m_row["preco"], "tags": m_row["tags_list"]},
                            after_state={"name": qe_name.strip(), "description": qe_desc.strip(), "price": qe_price, "tags": final_tags},
                            reversible=True,
                        )
                        st.session_state.creations = client.get_all_my_creations(currency=currency)
                        st.success("Modelo atualizado e backup guardado no Rollback!")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


# ==============================================================================
# ABA 2: ESTÚDIO DE PUBLICAÇÃO (DRAG & DROP DIRETO + SEO AUTOMÁTICO)
# ==============================================================================
with tab_publish:
    st.subheader("🚀 Estúdio de Publicação — Arrastar & Largar Direto")

    with bot._lock:
        presets_dict = dict(bot.presets)

    selected_preset_name = st.selectbox(
        "Selecionar Gama / Template de Produto",
        ["(Personalizado / Em branco)"] + list(presets_dict.keys()),
        help="Preenche automaticamente parâmetros de impressão, FAQ anti-dúvidas, preço e tags.",
    )
    active_preset = presets_dict.get(selected_preset_name, {})

    cats = st.session_state.categories
    cat_map = {c["label"]: c["id"] for c in cats} if cats else {}

    col_form, col_preview = st.columns([1.45, 1])

    with col_form:
        p_name = st.text_input("Título do Modelo *", placeholder="Ex: Seat Leon Mk2 (1P) - 52mm Air Vent Turbo Gauge Pod")

        c1, c2, c3 = st.columns(3)
        if cat_map:
            default_cat_idx = 0
            hint = str(active_preset.get("category_hint", "")).lower()
            if hint:
                for idx_c, lbl_c in enumerate(cat_map.keys()):
                    if hint in lbl_c.lower():
                        default_cat_idx = idx_c
                        break
            selected_cat_label = c1.selectbox("Categoria *", list(cat_map.keys()), index=default_cat_idx)
            p_cat_id = cat_map[selected_cat_label]
        else:
            p_cat_id = c1.text_input("ID da Categoria *")

        p_price = c2.number_input(f"Preço ({currency})", min_value=0.0, step=0.5, value=float(active_preset.get("price", 2.90)))
        p_locale = c3.selectbox("Idioma", ["EN", "PT", "ES", "FR", "DE"], index=0)

        st.markdown("#### 🖼️ Fotos & Renders do Modelo (Arrasta para aqui)")
        uploaded_images = st.file_uploader(
            "Arrasta as fotos/renders do modelo (a 1.ª imagem será a capa principal)",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key="pub_img_uploader",
        )
        if uploaded_images:
            img_cols = st.columns(min(4, len(uploaded_images)))
            for idx_img, up_img in enumerate(uploaded_images):
                with img_cols[idx_img % 4]:
                    st.image(up_img, caption=f"{'⭐ Capa' if idx_img == 0 else f'Foto {idx_img + 1}'}", use_container_width=True)

        st.markdown("#### 📦 Ficheiros 3D para Impressão (Arrasta para aqui)")
        uploaded_3d_files = st.file_uploader(
            "Arrasta os ficheiros STL, 3MF, OBJ, STEP ou ZIP",
            type=["stl", "3mf", "obj", "step", "stp", "zip"],
            accept_multiple_files=True,
            key="pub_3d_uploader",
        )
        if uploaded_3d_files:
            for up_f in uploaded_3d_files:
                size_kb = round(up_f.size / 1024.0, 1)
                st.markdown(f"<div class='upload-badge'>📄 <b>{up_f.name}</b> ({size_kb} KB) — Pronto para envio automático</div>", unsafe_allow_html=True)

        with st.expander("🔗 Opcional: Adicionar também links HTTPS externos manualmente"):
            manual_files_txt = st.text_area("URLs Ficheiros 3D (1 por linha)", height=60)
            manual_imgs_txt = st.text_area("URLs Imagens (1 por linha)", height=60)

        st.markdown("#### 📝 Descrição Estruturada & Tags")
        d_overview = st.text_area("1. Resumo & Funcionalidades *", value=active_preset.get("overview", ""), height=75)
        d_settings = st.text_area("2. Definições de Impressão 3D *", value=active_preset.get("print_settings", ""), height=95)
        d_assembly = st.text_area("3. Montagem & Hardware", value=active_preset.get("assembly", ""), height=55)
        d_faq = st.text_area("4. FAQ Anti-Dúvidas (reduz abandono)", value=active_preset.get("faq", ""), height=60)
        d_license = st.text_input("5. Aviso de Licença", value=active_preset.get("license", "Personal use only."))

        auto_imperial_pub = st.checkbox("Converter medidas mm ➔ polegadas automaticamente (ex: 8mm ➔ 8mm (5/16\"))", value=True)
        p_tags = st.text_input("Tags SEO (até 20, separadas por vírgula) *", value=active_preset.get("tags", ""))

    # Construir descrição e tags otimizadas em tempo real
    raw_sections = [d_overview.strip()]
    if d_settings.strip():
        raw_sections.append(f"### Print Settings\n{d_settings.strip()}")
    if d_assembly.strip():
        raw_sections.append(f"### Assembly & Hardware\n{d_assembly.strip()}")
    if d_faq.strip():
        raw_sections.append(f"### FAQ & Fitment Notes\n{d_faq.strip()}")
    if d_license.strip():
        raw_sections.append(f"### License\n{d_license.strip()}")
    composed_desc = "\n\n".join(s for s in raw_sections if s)

    final_pub_name = SeoOptimizerEngine.enrich_metric_units(p_name.strip()) if auto_imperial_pub else p_name.strip()
    final_pub_desc = SeoOptimizerEngine.enrich_metric_units(composed_desc) if auto_imperial_pub else composed_desc
    base_tag_list = [t.strip() for t in p_tags.split(",") if t.strip()]
    enriched_pub_tags = SeoOptimizerEngine.build_20_tags(final_pub_name, final_pub_desc, base_tag_list, max_tags=20)
    seo_report = SeoOptimizerEngine.analyze_listing(final_pub_name, final_pub_desc, enriched_pub_tags, p_price)

    with col_preview:
        st.markdown("#### 🎯 Diagnóstico SEO em Direto")
        st.progress(seo_report["score"] / 100.0, text=f"Qualidade SEO: {seo_report['score']}/100 ({len(enriched_pub_tags)}/20 tags)")
        st.caption("**Top 20 Tags Ordenadas por Peso que serão enviadas:**")
        st.code(", ".join(enriched_pub_tags), language=None)
        for tip in seo_report["tips"]:
            st.caption(f"⚠️ {tip}")

        st.markdown("#### 👁️ Pré-visualização Final")
        st.markdown(f"<div class='seo-card'><b>{final_pub_name or 'Título do Modelo'}</b><br><br>{final_pub_desc.replace(chr(10), '<br>')}</div>", unsafe_allow_html=True)

        with st.expander("💾 Guardar como Nova Gama"):
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
                            "faq": d_faq,
                            "license": d_license,
                        }
                    bot._save_state()
                    st.success("Preset guardado!")
                    st.rerun()

    st.divider()
    golden_utc = next_golden_hour_utc()
    golden_local_str = golden_utc.astimezone().strftime("%a %d/%m às %H:%M")

    act_p1, act_p2, act_p3 = st.columns([1.1, 1.2, 1.2])
    sched_mode = act_p2.selectbox(
        "Modo de Agendamento",
        [f"🌟 Hora de Ouro ({golden_local_str})", "⏱️ Daqui a 2 horas", "⏱️ Daqui a 6 horas", "⏱️ Daqui a 24 horas"],
    )

    def prepare_uploaded_urls() -> Tuple[List[str], List[str]]:
        f_urls = [u.strip() for u in manual_files_txt.splitlines() if u.strip()]
        i_urls = [u.strip() for u in manual_imgs_txt.splitlines() if u.strip()]

        for up_img in (uploaded_images or []):
            hosted_url = CloudAssetUploader.upload_bytes(up_img.getvalue(), up_img.name, up_img.type or "image/jpeg")
            i_urls.append(hosted_url)

        for up_f in (uploaded_3d_files or []):
            hosted_url = CloudAssetUploader.upload_bytes(up_f.getvalue(), up_f.name, up_f.type or "application/octet-stream")
            f_urls.append(hosted_url)

        return f_urls, i_urls

    if act_p1.button("🚀 Publicar Agora no Cults3D", type="primary", use_container_width=True):
        if not final_pub_name or not final_pub_desc or not p_cat_id:
            st.warning("Preenche o Título, Categoria e Descrição.")
        elif not (uploaded_images or manual_imgs_txt.strip()) or not (uploaded_3d_files or manual_files_txt.strip()):
            st.warning("Arrasta pelo menos 1 foto do modelo e 1 ficheiro 3D (STL/3MF/ZIP).")
        else:
            with st.spinner("A carregar fotos e ficheiros 3D para a cloud e a publicar no Cults3D..."):
                try:
                    file_list, img_list = prepare_uploaded_urls()
                    created = client.create_creation(
                        name=final_pub_name,
                        description=final_pub_desc,
                        category_id=str(p_cat_id),
                        file_urls=file_list,
                        image_urls=img_list,
                        price=p_price,
                        currency=currency,
                        tags=enriched_pub_tags,
                        locale=p_locale,
                    )
                    st.session_state.creations = client.get_all_my_creations(currency=currency)
                    st.success(f"Modelo publicado com sucesso! Link: {created.get('url')}")
                except Exception as e:
                    st.error(str(e))

    if act_p3.button("🗓️ Agendar na Fila do Bot", use_container_width=True):
        if not final_pub_name or not final_pub_desc or not p_cat_id:
            st.warning("Preenche o Título, Categoria e Descrição.")
        elif not (uploaded_images or manual_imgs_txt.strip()) or not (uploaded_3d_files or manual_files_txt.strip()):
            st.warning("Arrasta pelo menos 1 foto do modelo e 1 ficheiro 3D (STL/3MF/ZIP).")
        else:
            with st.spinner("A preparar ficheiros e a agendar na fila..."):
                try:
                    file_list, img_list = prepare_uploaded_urls()
                    if "Hora de Ouro" in sched_mode:
                        sched_dt = golden_utc
                    elif "6 horas" in sched_mode:
                        sched_dt = datetime.now(timezone.utc) + timedelta(hours=6)
                    elif "24 horas" in sched_mode:
                        sched_dt = datetime.now(timezone.utc) + timedelta(hours=24)
                    else:
                        sched_dt = datetime.now(timezone.utc) + timedelta(hours=2)

                    with bot._lock:
                        bot.publish_queue.append({
                            "queue_id": f"q_{int(time.time() * 1000)}",
                            "name": final_pub_name,
                            "description": final_pub_desc,
                            "category_id": str(p_cat_id),
                            "price": float(p_price),
                            "locale": p_locale,
                            "tags": enriched_pub_tags,
                            "file_urls": file_list,
                            "image_urls": img_list,
                            "scheduled_at": sched_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "status": "Pendente",
                        })
                    bot._save_state()
                    st.success(f"Agendado para {sched_dt.strftime('%Y-%m-%d %H:%M UTC')}!")
                except Exception as e:
                    st.error(str(e))


# ==============================================================================
# ABA 3: ESTÚDIO DE GESTÃO & SEO EM MASSA (BOMBASTIC & REVERSÍVEL)
# ==============================================================================
with tab_bulk:
    if df_creations.empty:
        st.info("Não tens modelos carregados.")
    else:
        st.subheader("⚡ Estúdio de Edição & SEO em Massa (com Backup & Rollback)")

        # Filtro inteligente de seleção
        f_col1, f_col2 = st.columns([1.5, 2.5])
        quick_filter = f_col1.selectbox(
            "Seleção Rápida Inteligente",
            [
                "Seleção Manual",
                "Todos os Modelos",
                "SEO Fraco (< 75/100)",
                "Poucas Tags (< 15 tags)",
                "Muito Vistos / 0 Vendas",
                "Preços Baixos (< 2.50)",
            ],
        )

        options_map = {
            f"{r['nome']} [SEO {r['seo_score']}/100 | {r['num_tags']} tags | {r['preco']:.2f} {currency}]": r
            for _, r in df_creations.iterrows()
        }
        all_labels = list(options_map.keys())

        default_selected: List[str] = []
        if quick_filter == "Todos os Modelos":
            default_selected = all_labels
        elif quick_filter == "SEO Fraco (< 75/100)":
            default_selected = [k for k, v in options_map.items() if v["seo_score"] < 75]
        elif quick_filter == "Poucas Tags (< 15 tags)":
            default_selected = [k for k, v in options_map.items() if v["num_tags"] < 15]
        elif quick_filter == "Muito Vistos / 0 Vendas":
            sales_ids = set(df_sales["creation_id"].unique()) if not df_sales.empty else set()
            default_selected = [k for k, v in options_map.items() if v["views"] >= 50 and v["id"] not in sales_ids and v["preco"] > 0]
        elif quick_filter == "Preços Baixos (< 2.50)":
            default_selected = [k for k, v in options_map.items() if 0 < v["preco"] < 2.50]

        selected_labels = f_col2.multiselect(
            f"Modelos Alvo ({len(default_selected)} pré-selecionados)",
            all_labels,
            default=default_selected,
        )

        b_seo, b_text, b_tags, b_price = st.tabs([
            "🧠 Auto-SEO em Massa (20 Tags + Medidas)",
            "📝 Localizar/Substituir & Blocos",
            "🏷️ Gestor Cirúrgico de Tags",
            "💰 Preços Psicológicos & Promos",
        ])

        # --- SUB-TAB 1: AUTO-SEO EM MASSA ---
        with b_seo:
            st.markdown("Otimiza automaticamente títulos, medidas (mm ➔ polegadas), bloco técnico de impressão e preenche até às **20 tags ordenadas por relevância**.")
            sc1, sc2, sc3 = st.columns(3)
            opt_imperial = sc1.checkbox("Converter mm ➔ polegadas", value=True)
            opt_print_blk = sc2.checkbox("Injetar Print Settings se faltar", value=True)
            opt_priority_tags = sc3.text_input("Tags prioritárias a forçar no Top 5", placeholder="ex: fsdesigns, petg")

            if selected_labels:
                preview_rows = []
                computed_updates: List[Dict[str, Any]] = []
                extra_t_list = [t.strip() for t in opt_priority_tags.split(",") if t.strip()]

                for lbl in selected_labels:
                    row = options_map[lbl]
                    new_n, new_d, new_t = SeoOptimizerEngine.auto_optimize_listing(
                        row["nome"],
                        row["descricao"],
                        row["tags_list"],
                        add_imperial=opt_imperial,
                        ensure_print_block=opt_print_blk,
                        extra_tags=extra_t_list,
                    )
                    new_eval = SeoOptimizerEngine.analyze_listing(new_n, new_d, new_t, row["preco"])
                    preview_rows.append({
                        "Modelo Atual": row["nome"],
                        "SEO Antes": f"{row['seo_score']}/100",
                        "SEO Depois": f"{new_eval['score']}/100",
                        "Tags": f"{row['num_tags']} ➔ {len(new_t)}",
                        "Top 5 Tags Novas": ", ".join(new_t[:5]),
                    })
                    computed_updates.append({
                        "row": row,
                        "new_name": new_n,
                        "new_desc": new_d,
                        "new_tags": new_t,
                    })

                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

                if st.button(f"🚀 Aplicar Otimização SEO em {len(computed_updates)} Modelo(s)", type="primary", use_container_width=True):
                    prog = st.progress(0.0)
                    done_cnt = 0
                    for idx_u, item_u in enumerate(computed_updates):
                        r = item_u["row"]
                        try:
                            client.update_creation(
                                creation_id=r["id"],
                                name=item_u["new_name"],
                                description=item_u["new_desc"],
                                tags=item_u["new_tags"],
                            )
                            bot.record_action(
                                "BULK_SEO",
                                r["id"],
                                r["nome"],
                                f"Auto-SEO ({len(item_u['new_tags'])} tags)",
                                before_state={"name": r["nome"], "description": r["descricao"], "tags": r["tags_list"]},
                                after_state={"name": item_u["new_name"], "description": item_u["new_desc"], "tags": item_u["new_tags"]},
                                reversible=True,
                            )
                            done_cnt += 1
                        except Exception as exc:
                            st.error(f"Erro em '{r['nome']}': {exc}")
                        prog.progress((idx_u + 1) / len(computed_updates))

                    st.session_state.creations = client.get_all_my_creations(currency=currency)
                    st.success(f"SEO otimizado com sucesso em {done_cnt} modelo(s)! Backup guardado no Rollback.")
                    st.rerun()

        # --- SUB-TAB 2: LOCALIZAR/SUBSTITUIR & INJETOR DE BLOCOS ---
        with b_text:
            tx_c1, tx_c2 = st.columns(2)
            with tx_c1:
                st.markdown("#### 🔎 Localizar & Substituir")
                fr_target = st.radio("Onde aplicar", ["Descrição", "Título"], horizontal=True)
                fr_find = st.text_input("Texto a procurar", placeholder="ex: PLA only")
                fr_replace = st.text_input("Substituir por", placeholder="ex: PETG / ASA / ABS recommended")
                if st.button("🔄 Executar Substituição", type="primary", use_container_width=True) and selected_labels and fr_find:
                    updated_c = 0
                    for lbl in selected_labels:
                        r = options_map[lbl]
                        if fr_target == "Descrição" and fr_find in r["descricao"]:
                            nd = r["descricao"].replace(fr_find, fr_replace)
                            client.update_creation(creation_id=r["id"], description=nd)
                            bot.record_action("BULK_REPLACE", r["id"], r["nome"], f"'{fr_find}' -> '{fr_replace}'", before_state={"description": r["descricao"]}, after_state={"description": nd}, reversible=True)
                            updated_c += 1
                        elif fr_target == "Título" and fr_find in r["nome"]:
                            nn = r["nome"].replace(fr_find, fr_replace)
                            client.update_creation(creation_id=r["id"], name=nn)
                            bot.record_action("BULK_REPLACE", r["id"], r["nome"], f"Título: '{fr_find}' -> '{fr_replace}'", before_state={"name": r["nome"]}, after_state={"name": nn}, reversible=True)
                            updated_c += 1
                    st.session_state.creations = client.get_all_my_creations(currency=currency)
                    st.success(f"Substituição aplicada em {updated_c} modelo(s)!")
                    st.rerun()

            with tx_c2:
                st.markdown("#### 🧩 Injetar / Atualizar Bloco de Conversão")
                blk_preset = st.selectbox(
                    "Template rápido de bloco",
                    [
                        "Banner Pack Completo (Upsell)",
                        "Garantia & Tolerâncias (Anti-Dúvidas)",
                        "Pedido de Make / Avaliação",
                        "Personalizado",
                    ],
                )
                default_blk = {
                    "Banner Pack Completo (Upsell)": "🔥 WANT THE FULL BUNDLE? Check my profile for the Complete Pack and save over 30%!",
                    "Garantia & Tolerâncias (Anti-Dúvidas)": "✅ FITMENT GUARANTEE: Engineered with 0.2mm clearance tolerances for accurate FDM printing. Message me anytime if you need custom dimensions!",
                    "Pedido de Make / Avaliação": "📸 PRINTED THIS MODEL? Upload a Make photo on Cults3D — I love seeing your builds in action!",
                    "Personalizado": "",
                }[blk_preset]
                blk_pos = st.radio("Posição na descrição", ["No Topo (máxima visibilidade)", "No Rodapé"], horizontal=True)
                blk_text = st.text_area("Conteúdo do bloco", value=default_blk, height=90)

                if st.button("➕ Aplicar Bloco nos Selecionados", type="primary", use_container_width=True) and selected_labels and blk_text.strip():
                    marker_start = "=== STUDIO BLOCK ==="
                    marker_end = "=== END STUDIO BLOCK ==="
                    wrapped_block = f"{marker_start}\n{blk_text.strip()}\n{marker_end}"
                    cnt_blk = 0
                    for lbl in selected_labels:
                        r = options_map[lbl]
                        clean_d = re.sub(r"\n*=== STUDIO BLOCK ===.*?=== END STUDIO BLOCK ===\n*", "\n\n", r["descricao"], flags=re.DOTALL).strip()
                        new_d = f"{wrapped_block}\n\n{clean_d}".strip() if "Topo" in blk_pos else f"{clean_d}\n\n{wrapped_block}".strip()
                        if new_d != r["descricao"].strip():
                            client.update_creation(creation_id=r["id"], description=new_d)
                            bot.record_action("BULK_BLOCK", r["id"], r["nome"], "Bloco atualizado", before_state={"description": r["descricao"]}, after_state={"description": new_d}, reversible=True)
                            cnt_blk += 1
                    st.session_state.creations = client.get_all_my_creations(currency=currency)
                    st.success(f"Bloco aplicado em {cnt_blk} modelo(s)!")
                    st.rerun()

        # --- SUB-TAB 3: GESTOR CIRÚRGICO DE TAGS ---
        with b_tags:
            tag_action = st.radio(
                "Operação de Tags",
                [
                    "Promover para Top 5 (maior peso no algoritmo) + Adicionar",
                    "Adicionar ao fim das existentes (até 20)",
                    "Remover tags específicas",
                    "Substituir todas as tags",
                ],
            )
            tags_field = st.text_input("Tags (separadas por vírgula)", placeholder="ex: seat leon, 1p, gauge pod, 52mm")
            if st.button("🏷️ Aplicar Alteração de Tags", type="primary", use_container_width=True) and selected_labels and tags_field.strip():
                in_tags = [t.strip().lower() for t in tags_field.split(",") if t.strip()]
                t_cnt = 0
                for lbl in selected_labels:
                    r = options_map[lbl]
                    cur_t = [t.strip().lower() for t in r["tags_list"] if t.strip()]
                    if "Promover" in tag_action:
                        merged = list(dict.fromkeys(in_tags + cur_t))[:20]
                    elif "Adicionar" in tag_action:
                        merged = list(dict.fromkeys(cur_t + in_tags))[:20]
                    elif "Remover" in tag_action:
                        rem_set = set(in_tags)
                        merged = [t for t in cur_t if t not in rem_set]
                    else:
                        merged = list(dict.fromkeys(in_tags))[:20]

                    if merged != cur_t:
                        client.update_creation(creation_id=r["id"], tags=merged)
                        bot.record_action("BULK_TAGS", r["id"], r["nome"], f"{len(cur_t)} -> {len(merged)} tags", before_state={"tags": r["tags_list"]}, after_state={"tags": merged}, reversible=True)
                        t_cnt += 1
                st.session_state.creations = client.get_all_my_creations(currency=currency)
                st.success(f"Tags atualizadas em {t_cnt} modelo(s)!")
                st.rerun()

        # --- SUB-TAB 4: PREÇOS PSICOLÓGICOS & PROMOÇÕES ---
        with b_price:
            pc1, pc2 = st.columns(2)
            with pc1:
                st.markdown("#### 💶 Preços & Arredondamento Psicológico")
                p_strategy = st.selectbox(
                    "Estratégia",
                    [
                        "Arredondar para .90 (ex: 2.30 -> 2.90)",
                        "Arredondar para .99 (ex: 2.30 -> 2.99)",
                        "Definir Piso Mínimo (proteger margem contra taxas)",
                        "Ajuste Percentual (%)",
                        "Preço Fixo Novo",
                    ],
                )
                p_param = st.number_input("Valor / Piso / Percentagem", value=2.90, step=0.5)
                if st.button("💾 Atualizar Preços Selecionados", type="primary", use_container_width=True) and selected_labels:
                    p_cnt = 0
                    for lbl in selected_labels:
                        r = options_map[lbl]
                        old_p = float(r["preco"])
                        if old_p == 0 and "Fixo" not in p_strategy:
                            continue
                        if ".90" in p_strategy:
                            new_p = round(int(old_p) + 0.90, 2)
                        elif ".99" in p_strategy:
                            new_p = round(int(old_p) + 0.99, 2)
                        elif "Piso" in p_strategy:
                            new_p = max(old_p, round(p_param, 2))
                        elif "Percentual" in p_strategy:
                            new_p = round(old_p * (1.0 + p_param / 100.0), 2)
                        else:
                            new_p = round(max(0.0, p_param), 2)

                        if abs(new_p - old_p) > 0.009:
                            client.update_creation(creation_id=r["id"], price=new_p, currency=currency)
                            bot.record_action("BULK_PRICE", r["id"], r["nome"], f"{old_p:.2f} -> {new_p:.2f} {currency}", before_state={"price": old_p}, after_state={"price": new_p}, reversible=True)
                            p_cnt += 1
                    st.session_state.creations = client.get_all_my_creations(currency=currency)
                    st.success(f"Preços atualizados em {p_cnt} modelo(s)!")
                    st.rerun()

            with pc2:
                st.markdown("#### 🔥 Campanha de Desconto em Massa")
                disc_pct = st.slider("Desconto (%)", 5, 90, 20, 5)
                disc_days = st.number_input("Duração (dias)", 1, 90, 5)
                if st.button("🔥 Ativar Desconto nos Selecionados", type="primary", use_container_width=True) and selected_labels:
                    end_at = (datetime.now(timezone.utc) + timedelta(days=int(disc_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
                    d_cnt = 0
                    for lbl in selected_labels:
                        r = options_map[lbl]
                        if r["preco"] > 0:
                            client.create_discount(creation_id=r["id"], percentage=disc_pct, end_at_iso=end_at)
                            d_cnt += 1
                    st.success(f"Desconto de {disc_pct}% ativado em {d_cnt} modelo(s)!")


# ==============================================================================
# ABA 4: BOT AUTÓNOMO & GROWTH ENGINE
# ==============================================================================
with tab_bot:
    st.subheader("🤖 Bot Autónomo Cults3D (Anti-Sleep + Growth Engine)")

    b1, b2, b3, b4, b5 = st.columns([1, 1, 1.2, 1, 1.3])
    b1.metric("Estado", "🟢 ATIVO" if bot.running else "🔴 PARADO")
    b2.metric("Modo", "🧪 Simulação" if bot.config["dry_run"] else "⚡ Real")
    b3.metric("Anti-Sleep", bot.keepalive_status)
    b4.metric("Último Ciclo", (bot.last_run or "Nunca").split(" ")[-1])

    with b5:
        if not bot.running:
            if st.button("▶️ Iniciar Bot 24/7", type="primary", use_container_width=True):
                bot.start()
                st.rerun()
        else:
            if st.button("⏹️ Parar Bot", use_container_width=True):
                bot.stop()
                st.rerun()

        if st.button("⚡ Correr 1 Ciclo Agora", use_container_width=True):
            with st.spinner("A executar ciclo completo..."):
                bot.run_cycle()
            st.rerun()

    sub_rules, sub_growth, sub_tg, sub_queue, sub_rollback, sub_logs = st.tabs([
        "⚙️ Regras & Anti-Sleep",
        "🚀 Growth: Early-Bird & Testes A/B",
        "📱 Comandos Telegram",
        "🗓️ Fila Agendada",
        "↩️ Rollback",
        "📜 Logs",
    ])

    with sub_rules:
        cfg = bot.config
        with st.form("full_bot_config_form"):
            st.markdown("#### 🛡️ Anti-Sleep Cloud, Motor & Notificações")
            as_c1, as_c2 = st.columns([2.2, 1])
            in_app_url = as_c1.text_input("URL Público da App (Anti-Sleep)", value=cfg["app_url"])
            in_as_on = as_c2.checkbox("Ativar Auto-Ping Anti-Sleep", value=bool(cfg["anti_sleep_enabled"]))

            m_c1, m_c2, m_c3 = st.columns(3)
            in_interval = m_c1.number_input("Intervalo (min)", min_value=2, max_value=1440, value=int(cfg["interval_minutes"]))
            in_max_act = m_c2.number_input("Máx. alterações/ciclo", min_value=1, max_value=20, value=int(cfg["max_actions_per_cycle"]))
            in_dry = m_c3.checkbox("Modo Simulação (Dry-Run)", value=bool(cfg["dry_run"]))

            n_c1, n_c2, n_c3 = st.columns(3)
            in_tg_tok = n_c1.text_input("Telegram Bot Token", value=cfg["tg_token"], type="password")
            in_tg_chat = n_c2.text_input("Telegram Chat ID", value=cfg["tg_chat_id"])
            in_disc = n_c3.text_input("Discord Webhook URL", value=cfg["discord_webhook"], type="password")

            st.divider()
            st.markdown("#### 1️⃣ Vendas, Kit Social & Assistente Pós-Venda")
            s_c1, s_c2, s_c3, s_c4 = st.columns(4)
            in_r_sales = s_c1.checkbox("Alertar Vendas", value=bool(cfg["rule_sales_alert"]))
            in_r_postsale = s_c2.checkbox("Script Pós-Venda", value=bool(cfg["rule_post_sale_script"]))
            in_r_social = s_c3.checkbox("Kit Social (Reddit/IG)", value=bool(cfg["rule_social_kit"]))
            in_r_mile = s_c4.checkbox("Celebrar Marcos", value=bool(cfg["rule_milestone_alert"]))

            st.divider()
            st.markdown("#### 2️⃣ Vitrine Dinâmica & SEO Automático")
            v_c1, v_c2, v_c3 = st.columns([1.3, 0.8, 1.9])
            in_r_show = v_c1.checkbox("Vitrine Dinâmica", value=bool(cfg["rule_dynamic_showcase"]))
            in_show_n = v_c2.number_input("Nº Top Modelos", min_value=1, max_value=5, value=int(cfg["showcase_top_n"]))
            in_show_title = v_c3.text_input("Título da Vitrine", value=cfg["showcase_title"])

            in_r_seo = st.checkbox("Auto-SEO & 20 Tags nos Novos Modelos", value=bool(cfg["rule_seo_optimizer"]))
            seo_c1, seo_c2 = st.columns(2)
            in_seo_marker = seo_c1.text_input("Marcador rodapé", value=cfg["seo_footer_marker"])
            in_seo_tags = seo_c2.text_input("Tags prioritárias", value=cfg["seo_required_tags"])
            in_seo_footer = st.text_area("Texto rodapé", value=cfg["seo_footer_text"], height=65)

            st.divider()
            st.markdown("#### 3️⃣ Promoções & Preço Dinâmico")
            p_c1, p_c2, p_c3, p_c4, p_c5 = st.columns([1.2, 1, 1, 1, 1])
            in_r_promo = p_c1.checkbox("Auto-Promo (0 Vendas)", value=bool(cfg["rule_auto_promo"]))
            in_p_views = p_c2.number_input("Mín. Views", min_value=10, max_value=50000, value=int(cfg["promo_min_views"]))
            in_p_pct = p_c3.number_input("Desconto (%)", min_value=5, max_value=90, value=int(cfg["promo_discount_pct"]))
            in_p_days = p_c4.number_input("Duração (dias)", min_value=1, max_value=30, value=int(cfg["promo_days"]))
            in_p_cd = p_c5.number_input("Cooldown (dias)", min_value=1, max_value=90, value=int(cfg["promo_cooldown_days"]))

            st_c1, st_c2, st_c3, st_c4 = st.columns([1.3, 1, 1, 1])
            in_r_stag = st_c1.checkbox("Reativar Estagnados", value=bool(cfg["rule_stagnant_revival"]))
            in_stag_days = st_c2.number_input("Dias sem vender", min_value=7, max_value=365, value=int(cfg["stagnant_days"]))
            in_stag_pct = st_c3.number_input("Desconto (%)", min_value=5, max_value=90, value=int(cfg["stagnant_discount_pct"]))
            in_stag_pdays = st_c4.number_input("Promo (dias)", min_value=1, max_value=14, value=int(cfg["stagnant_promo_days"]))

            d_c1, d_c2, d_c3, d_c4 = st.columns([1.2, 1, 1, 1])
            in_r_price = d_c1.checkbox("Preço Dinâmico", value=bool(cfg["rule_dynamic_price"]))
            in_d_step = d_c2.number_input("A cada X vendas", min_value=1, max_value=500, value=int(cfg["price_step_sales"]))
            in_d_pct = d_c3.number_input("Subida (%)", min_value=5.0, max_value=100.0, value=float(cfg["price_bump_pct"]), step=5.0)
            in_d_cap = d_c4.number_input(f"Teto Máx ({currency})", min_value=1.0, max_value=500.0, value=float(cfg["price_max_cap"]), step=1.0)

            w_c1, w_c2, w_c3, w_c4 = st.columns([1.3, 1, 1, 1])
            in_r_wk = w_c1.checkbox("Flash Fim-de-Semana", value=bool(cfg["rule_weekend_flash"]))
            dias_opt = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
            in_wk_day = w_c2.selectbox("Dia início", dias_opt, index=int(cfg["weekend_day"]))
            in_wk_hr = w_c3.number_input("Hora (0-23)", min_value=0, max_value=23, value=int(cfg["weekend_hour"]))
            in_wk_pct = w_c4.number_input("Desconto Flash (%)", min_value=5, max_value=80, value=int(cfg["weekend_discount_pct"]))

            st.divider()
            st.markdown("#### 4️⃣ Picos, Radar & Relatórios")
            sp_c1, sp_c2, rk_c1, rk_c2 = st.columns([1, 0.8, 1, 1.8])
            in_r_spike = sp_c1.checkbox("Picos Views", value=bool(cfg["rule_spike_alert"]))
            in_spike_val = sp_c2.number_input("Salto mín.", min_value=5, max_value=5000, value=int(cfg["spike_views_threshold"]))
            in_r_mkt = rk_c1.checkbox("Radar Nichos", value=bool(cfg["rule_market_watch"]))
            in_kw = rk_c2.text_input("Keywords vigiadas (máx 4)", value=cfg["watch_keywords"])

            rep_c1, rep_c2, rep_c3, rep_c4 = st.columns(4)
            in_r_rep = rep_c1.checkbox("Relatório Diário", value=bool(cfg["rule_daily_report"]))
            in_rep_hr = rep_c2.number_input("Hora envio", min_value=0, max_value=23, value=int(cfg["daily_report_hour"]))
            in_r_wk_rep = rep_c3.checkbox("Resumo Semanal", value=bool(cfg["rule_weekly_report"]))
            in_r_queue = rep_c4.checkbox("Fila Publicação", value=bool(cfg["rule_queue_publisher"]))

            save_all_btn = st.form_submit_button("💾 Guardar Toda a Configuração", type="primary", use_container_width=True)

        if save_all_btn:
            with bot._lock:
                bot.config.update({
                    "app_url": in_app_url.strip(),
                    "anti_sleep_enabled": bool(in_as_on),
                    "interval_minutes": int(in_interval),
                    "max_actions_per_cycle": int(in_max_act),
                    "dry_run": bool(in_dry),
                    "tg_token": in_tg_tok.strip(),
                    "tg_chat_id": in_tg_chat.strip(),
                    "discord_webhook": in_disc.strip(),
                    "rule_sales_alert": bool(in_r_sales),
                    "rule_post_sale_script": bool(in_r_postsale),
                    "rule_social_kit": bool(in_r_social),
                    "rule_milestone_alert": bool(in_r_mile),
                    "rule_dynamic_showcase": bool(in_r_show),
                    "showcase_top_n": int(in_show_n),
                    "showcase_title": in_show_title.strip(),
                    "rule_seo_optimizer": bool(in_r_seo),
                    "seo_footer_marker": in_seo_marker.strip(),
                    "seo_required_tags": in_seo_tags.strip(),
                    "seo_footer_text": in_seo_footer,
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
                    "rule_spike_alert": bool(in_r_spike),
                    "spike_views_threshold": int(in_spike_val),
                    "rule_market_watch": bool(in_r_mkt),
                    "watch_keywords": in_kw.strip(),
                    "rule_daily_report": bool(in_r_rep),
                    "daily_report_hour": int(in_rep_hr),
                    "rule_weekly_report": bool(in_r_wk_rep),
                    "rule_queue_publisher": bool(in_r_queue),
                })
            bot.ping_keep_alive()
            bot.log("CONFIG", "Configuração guardada com sucesso.")
            st.success("Configuração guardada!")

        c_t1, c_t2, c_t3 = st.columns(3)
        if c_t1.button("🔔 Testar Telegram/Discord", use_container_width=True):
            bot.send_notification("✅ <b>Cults3D Studio Pro v5:</b> Notificações operacionais!")
            st.toast("Notificação enviada!")
        if c_t2.button("🛡️ Testar Ping Anti-Sleep", use_container_width=True):
            bot.ping_keep_alive()
            st.toast(f"Anti-Sleep: {bot.keepalive_status}")
            st.rerun()
        if c_t3.button("🧹 Limpar Cooldowns Promo", use_container_width=True):
            with bot._lock:
                bot.promo_cooldowns = {}
            bot._save_state()
            st.toast("Cooldowns limpos!")

    with sub_growth:
        if df_creations.empty:
            st.info("Carrega primeiro os teus modelos.")
        else:
            model_options = {f"{r['nome']} (atual: {r['preco']:.2f} {currency})": r for _, r in df_creations.iterrows()}
            g_col1, g_col2 = st.columns(2)

            with g_col1:
                st.markdown("#### 🐣 Preço Early-Bird de Lançamento")
                with st.form("eb_form"):
                    eb_model_lbl = st.selectbox("Modelo alvo", list(model_options.keys()), key="eb_sel")
                    eb_launch = st.number_input(f"Preço Lançamento ({currency})", min_value=0.5, step=0.5, value=1.99)
                    eb_regular = st.number_input(f"Preço Normal Final ({currency})", min_value=1.0, step=0.5, value=3.99)
                    eb_sales = st.number_input("Subir após quantas vendas?", min_value=1, max_value=50, value=3)
                    if st.form_submit_button("➕ Ativar Regra Early-Bird", type="primary", use_container_width=True):
                        m_obj = model_options[eb_model_lbl]
                        with bot._lock:
                            bot.early_bird_rules[m_obj["id"]] = {
                                "name": m_obj["nome"],
                                "launch_price": float(eb_launch),
                                "regular_price": float(eb_regular),
                                "target_sales": int(eb_sales),
                                "completed": False,
                            }
                        bot._save_state()
                        st.success(f"Early-Bird ativo para '{m_obj['nome']}'!")
                        st.rerun()

                if bot.early_bird_rules:
                    st.dataframe(
                        pd.DataFrame([
                            {"Modelo": v["name"], "Lançamento": v["launch_price"], "Final": v["regular_price"], "Meta": v["target_sales"], "Estado": "Concluído" if v.get("completed") else "Ativo"}
                            for v in bot.early_bird_rules.values()
                        ]),
                        use_container_width=True,
                        hide_index=True,
                    )

            with g_col2:
                st.markdown("#### 🧪 Teste A/B de Preço por RPM")
                with st.form("ab_form"):
                    ab_model_lbl = st.selectbox("Modelo a testar", list(model_options.keys()), key="ab_sel")
                    ab_pa = st.number_input(f"Preço A ({currency})", min_value=0.5, step=0.5, value=2.50)
                    ab_pb = st.number_input(f"Preço B ({currency})", min_value=0.5, step=0.5, value=4.00)
                    ab_days = st.number_input("Dias por cada preço", min_value=2, max_value=30, value=7)
                    if st.form_submit_button("🧪 Iniciar Teste A/B", type="primary", use_container_width=True):
                        m_obj = model_options[ab_model_lbl]
                        cur_rev = float(df_sales[df_sales["creation_id"] == m_obj["id"]]["receita_liquida"].sum()) if not df_sales.empty else 0.0
                        with bot._lock:
                            bot.ab_tests[m_obj["id"]] = {
                                "name": m_obj["nome"],
                                "price_a": float(ab_pa),
                                "price_b": float(ab_pb),
                                "days_per_phase": int(ab_days),
                                "phase": "A",
                                "phase_start": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "start_views": int(m_obj["views"]),
                                "start_rev": cur_rev,
                                "rpm_a": 0.0,
                                "rpm_b": 0.0,
                                "winner_price": 0.0,
                            }
                        bot._save_state()
                        st.success(f"Teste A/B iniciado para '{m_obj['nome']}'!")
                        st.rerun()

                if bot.ab_tests:
                    st.dataframe(
                        pd.DataFrame([
                            {"Modelo": v["name"], "Fase": v["phase"], "Preço A": v["price_a"], "Preço B": v["price_b"], "RPM A": v.get("rpm_a", 0), "RPM B": v.get("rpm_b", 0), "Vencedor": v.get("winner_price") or "-"}
                            for v in bot.ab_tests.values()
                        ]),
                        use_container_width=True,
                        hide_index=True,
                    )

    with sub_tg:
        st.markdown(
            """
            - `/status` — Estado do bot, Anti-Sleep, último ciclo e quota da API.
            - `/vendas` — Últimas 5 vendas com receita líquida e comprador.
            - `/top` — Top 5 modelos mais vistos e downloads.
            - `/kit` — Post pronto a copiar/colar para o teu modelo mais visto.
            - `/ciclo` — Força um ciclo completo imediato.
            - `/pausar` e `/retomar` — Pausa ou volta a ligar o bot pelo telemóvel.
            """
        )
        if st.button("📡 Ler Comandos Pendentes Agora"):
            bot.poll_telegram_commands()
            st.toast("Comandos verificados!")

    with sub_queue:
        if bot.publish_queue:
            st.dataframe(
                pd.DataFrame([
                    {"Modelo": x["name"], "Preço": x["price"], "Agendado (UTC)": x["scheduled_at"], "Estado": x["status"]}
                    for x in bot.publish_queue
                ]),
                use_container_width=True,
                hide_index=True,
            )
            if st.button("🗑️ Limpar Concluídos"):
                with bot._lock:
                    bot.publish_queue = [x for x in bot.publish_queue if x.get("status") == "Pendente"]
                bot._save_state()
                st.rerun()
        else:
            st.info("Fila vazia.")

    with sub_rollback:
        with bot._lock:
            hist_items = list(bot.action_history)
        if not hist_items:
            st.info("Sem alterações registadas.")
        else:
            st.dataframe(
                pd.DataFrame([
                    {"Hora": h["hora"], "Tipo": h["tipo"], "Modelo": h["modelo"], "Detalhe": h["detalhe"], "Revertido": "Sim" if h.get("reverted") else "Não"}
                    for h in hist_items
                ]),
                use_container_width=True,
                hide_index=True,
            )
            reversible_list = [h for h in hist_items if h.get("reversible") and not h.get("reverted")]
            if reversible_list:
                rev_map = {f"[{h['hora']}] {h['tipo']} — {h['modelo']} ({h['detalhe']})": h["action_id"] for h in reversible_list}
                sel_rev = st.selectbox("Selecionar alteração para desfazer", list(rev_map.keys()))
                if st.button("↩️ Desfazer Agora", type="primary"):
                    try:
                        st.success(bot.revert_action(rev_map[sel_rev]))
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    with sub_logs:
        l1, l2 = st.columns(2)
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
            st.info("Sem logs.")


# ==============================================================================
# ABA 5: RADAR DE MERCADO & EXTRATOR DE N-GRAMS
# ==============================================================================
with tab_market:
    st.subheader("🔍 Radar de Nichos & Mineração de Keywords (N-Grams)")
    r_c1, r_c2, r_c3, r_c4 = st.columns([2.5, 1.2, 1, 1])
    m_query = r_c1.text_input("Nicho / Palavra-chave *", placeholder="Ex: seat leon, gauge pod, trim clip")
    m_price_filter = r_c2.selectbox("Filtro", ["Apenas Pagos", "Todos (Pagos + Grátis)", "Apenas Gratuitos"])
    m_limit = r_c3.number_input("Amostra", 10, 100, 40, 10)
    m_sort = r_c4.selectbox("Ordenar por", ["Receita Estimada", "Downloads", "Likes", "Views", "Preço"])

    if st.button("🔎 Analisar & Extrair Keywords", type="primary") and m_query.strip():
        with st.spinner(f"A minerar dados de '{m_query.strip()}'..."):
            try:
                raw_search = client.search_market(
                    search_query=m_query.strip(),
                    limit=int(m_limit),
                    only_priced=(m_price_filter == "Apenas Pagos"),
                    currency=currency,
                )
                total_in_cults = raw_search.get("total", 0)
                results = raw_search.get("results") or []

                rows = []
                all_niche_tags: List[str] = []
                top_titles: List[str] = []

                for r in results:
                    p_val = round(((r.get("price") or {}).get("cents") or 0) / 100.0, 2)
                    if m_price_filter == "Apenas Gratuitos" and p_val > 0:
                        continue
                    v_cnt = int(r.get("viewsCount") or 0)
                    l_cnt = int(r.get("likesCount") or 0)
                    d_cnt = int(r.get("downloadsCount") or 0)
                    est_rev = round(p_val * d_cnt, 2)
                    r_tags = [str(t).strip().lower() for t in (r.get("tags") or []) if str(t).strip()]
                    all_niche_tags.extend(r_tags)
                    if d_cnt > 0 or l_cnt > 2:
                        top_titles.append(r.get("name") or "")

                    rows.append({
                        "nome": r.get("name") or "",
                        "criador": (r.get("creator") or {}).get("nick") or "?",
                        "preco": p_val,
                        "views": v_cnt,
                        "likes": l_cnt,
                        "downloads": d_cnt,
                        "receita_est": est_rev,
                        "url": r.get("url") or "",
                    })

                if not rows:
                    st.info("Sem resultados para estes filtros.")
                else:
                    df_m = pd.DataFrame(rows)
                    paid_sub = df_m[df_m["preco"] > 0]
                    avg_p = float(paid_sub["preco"].mean()) if not paid_sub.empty else 0.0
                    med_p = float(paid_sub["preco"].median()) if not paid_sub.empty else 0.0
                    avg_dl = float(df_m["downloads"].mean())
                    tot_rev = float(df_m["receita_est"].sum())
                    opp_score = int(max(10, min(100, round(min(60.0, avg_dl * 1.5 + (tot_rev / max(1, len(df_m))) * 0.5) + (40.0 - min(40.0, (total_in_cults / 500.0) * 40.0))))))

                    mk1, mk2, mk3, mk4, mk5 = st.columns(5)
                    mk1.metric("Total no Cults3D", f"{total_in_cults:,}")
                    mk2.metric("Índice Oportunidade", f"{opp_score}/100")
                    mk3.metric("Preço Médio / Mediana", f"{avg_p:.2f} / {med_p:.2f} {currency}")
                    mk4.metric("Média Downloads", f"{avg_dl:.1f}")
                    mk5.metric("Receita Est. Amostra", f"{tot_rev:,.2f} {currency}")

                    st.divider()
                    ng1, ng2 = st.columns(2)
                    with ng1:
                        st.markdown("#### 🧠 Combinações de Palavras dos Líderes (N-Grams)")
                        bigrams = extract_ngrams(top_titles or df_m["nome"].tolist(), n=2, top_k=8)
                        trigrams = extract_ngrams(top_titles or df_m["nome"].tolist(), n=3, top_k=5)
                        if bigrams:
                            st.write("**Expressões de 2 palavras mais lucrativas:**")
                            st.code(", ".join(f"{g} ({c}x)" for g, c in bigrams), language=None)
                        if trigrams:
                            st.write("**Expressões de 3 palavras (Cauda Longa):**")
                            st.code(", ".join(f"{g} ({c}x)" for g, c in trigrams), language=None)

                    with ng2:
                        st.markdown("#### 🏷️ Top 20 Tags dos Líderes Prontas a Copiar")
                        if all_niche_tags:
                            top_tags = [t for t, _ in Counter(all_niche_tags).most_common(20)]
                            st.code(", ".join(top_tags), language=None)

                    sort_map = {"Receita Estimada": "receita_est", "Downloads": "downloads", "Likes": "likes", "Views": "views", "Preço": "preco"}
                    st.dataframe(
                        df_m.sort_values(by=sort_map[m_sort], ascending=False),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "preco": st.column_config.NumberColumn(f"Preço ({currency})", format="%.2f"),
                            "receita_est": st.column_config.NumberColumn(f"Receita Est. ({currency})", format="%.2f"),
                            "url": st.column_config.LinkColumn("Link", display_text="Ver"),
                        },
                    )
            except Exception as e:
                st.error(str(e))


# ==============================================================================
# ABA 6: CONSOLA GRAPHQL
# ==============================================================================
with tab_console:
    st.subheader("🛠️ Consola GraphQL")
    col_g1, col_g2 = st.columns([2, 1])
    with col_g1:
        raw_query = st.text_area("Query ou Mutation", value="query {\n  myself {\n    user { nick url }\n    creationsBatch(limit: 5) {\n      total\n      results { id name viewsCount }\n    }\n  }\n}", height=180)
        raw_vars = st.text_input("Variables (JSON)", value="{}")
        if st.button("▶️ Executar"):
            try:
                st.json(client.execute(raw_query, json.loads(raw_vars) if raw_vars.strip() else {}))
            except Exception as e:
                st.error(str(e))
    with col_g2:
        type_to_check = st.text_input("Inspecionar Tipo", value="Mutation")
        if st.button("🔎 Inspecionar"):
            try:
                st.json(client.introspect_type(type_to_check.strip()))
            except Exception as e:
                st.error(str(e))