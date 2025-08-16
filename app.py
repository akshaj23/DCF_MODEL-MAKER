# app.py — DCF from SEC XBRL (CompanyFacts) + optional Damodaran
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import requests
import streamlit as st
import altair as alt

# yfinance is optional – app works without it
try:
    import yfinance as yf
except Exception:
    yf = None

# ─────────────────────────────────────────────────────────────
# Page / constants
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="DCF Valuation (SEC XBRL + Damodaran)", layout="wide")
DATA_DIR = Path("Data")
TICKERS_CSV = DATA_DIR / "sec_company_tickers.csv"

# Optional Damodaran files (safe to omit)
DAMO_MARGINS = DATA_DIR / "Operating&NetMarginsbyIndustry(US).xlsx"
DAMO_CAPEX   = DATA_DIR / "CapitalExpendituresbySector(US).xlsx"
DAMO_WC      = DATA_DIR / "WorkingCapitalComponentsPercentofSales.xlsx"
DAMO_BETAS   = DATA_DIR / "Industry-Betas.xlsx"
DAMO_COUNTRY = DATA_DIR / "CountryRiskPremiums.xlsx"

SEC_HEADERS = {"User-Agent": "DCFApp/1.0 (contact: your_email@example.com)"}  # put your email
FY_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A"}
EPS = 1e-9

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def nfloat(x) -> float:
    try:
        if x is None:
            return np.nan
        if isinstance(x, str) and x.strip().lower() in {"", "none", "nan", "na", "null"}:
            return np.nan
        return float(x)
    except Exception:
        try:
            return pd.to_numeric(x, errors="coerce")
        except Exception:
            return np.nan

def isnum(x) -> bool:
    x = nfloat(x)
    return not (pd.isna(x) or not np.isfinite(x))

def to_numeric_df(df: pd.DataFrame, except_cols=("fy",)) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    for c in out.columns:
        if c in except_cols:
            continue
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out

def human(x: Optional[float], digits=2, prefix="$"):
    if x is None or (isinstance(x, (float, int)) and (pd.isna(x) or not np.isfinite(x))):
        return "—"
    v = float(x)
    if abs(v) >= 1e12: return f"{prefix}{v/1e12:.{digits}f}T"
    if abs(v) >= 1e9:  return f"{prefix}{v/1e9:.{digits}f}B"
    if abs(v) >= 1e6:  return f"{prefix}{v/1e6:.{digits}f}M"
    if abs(v) >= 1e3:  return f"{prefix}{v/1e3:.{digits}f}k"
    return f"{prefix}{v:,.{digits}f}"

def human_inline(x: Optional[float]) -> str:
    if not isnum(x):
        return "—"
    v = float(x)
    return f"{v:,.0f} ({human(v,2)[1:]})"

def clamp(x, lo, hi):
    try:
        return float(min(max(float(x), lo), hi))
    except Exception:
        return float(lo)

# ─────────────────────────────────────────────────────────────
# Tickers
# ─────────────────────────────────────────────────────────────
@st.cache_data
def load_tickers() -> pd.DataFrame:
    def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
        cols = {c.lower().strip(): c for c in df.columns}
        def pick(*names):
            for n in names:
                if n in cols: return cols[n]
            return None
        tcol = pick("ticker","symbol","sym","ticker_symbol")
        ncol = pick("title","name","company")
        ccol = pick("cik_str","cik","cik number","ciknumber","cik#")
        if not all([tcol,ncol,ccol]):
            raise ValueError("sec_company_tickers.csv is missing ticker/title/cik_str columns.")
        out = df.rename(columns={tcol:"ticker", ncol:"title", ccol:"cik_str"})[["ticker","title","cik_str"]].copy()
        out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
        out["title"]  = out["title"].astype(str).str.strip()
        out["cik_str"]= out["cik_str"].astype(str).str.replace(r"\D","",regex=True).str.zfill(10)
        out = out.dropna().drop_duplicates(subset=["ticker"])
        out["label"] = out["title"] + " (" + out["ticker"] + ")"
        return out[["label","ticker","title","cik_str"]].sort_values("title")

    try:
        df = pd.read_csv(TICKERS_CSV, encoding="utf-8-sig", engine="python")
        return _normalize_cols(df)
    except Exception:
        url = "https://www.sec.gov/files/company_tickers.json"
        r = requests.get(url, headers=SEC_HEADERS, timeout=30)
        r.raise_for_status()
        raw = r.json()
        rows = [{"ticker": v.get("ticker","").upper(),
                 "title": v.get("title",""),
                 "cik_str": str(v.get("cik_str","")).zfill(10)} for v in raw.values()]
        df = pd.DataFrame(rows)
        norm = _normalize_cols(df)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            norm[["ticker","title","cik_str"]].to_csv(TICKERS_CSV, index=False)
        except Exception:
            pass
        return norm

# ─────────────────────────────────────────────────────────────
# Damodaran (optional → safe defaults)
# ─────────────────────────────────────────────────────────────
@st.cache_data
def load_damodaran():
    industries = set()
    dmap: Dict[str,dict] = {}

    def read_xlsx(p: Path) -> Optional[pd.DataFrame]:
        try:
            if p.exists(): return pd.read_excel(p)
        except Exception:
            pass
        return None

    # Operating & Net margins
    dm = read_xlsx(DAMO_MARGINS)
    if dm is not None and len(dm):
        dm.columns = [c.lower() for c in dm.columns]
        iname = next((c for c in dm.columns if "industry" in c or "sector" in c), dm.columns[0])
        opm_c = next((c for c in dm.columns if "operating" in c and "margin" in c), None) or \
                next((c for c in dm.columns if "ebit" in c and "margin" in c), None)
        net_c = next((c for c in dm.columns if "net" in c and "margin" in c), None)
        for _,r in dm.iterrows():
            name = str(r.get(iname,"")).strip()
            if not name: continue
            industries.add(name)
            if opm_c is not None:
                v = nfloat(r.get(opm_c));  v = (v/100.0 if isnum(v) and v>1.5 else v)
                dmap.setdefault(name, {})["op_margin"] = v
            if net_c is not None:
                v2 = nfloat(r.get(net_c)); v2 = (v2/100.0 if isnum(v2) and v2>1.5 else v2)
                dmap.setdefault(name, {})["net_margin"] = v2

    # CapEx & Dep%
    cap = read_xlsx(DAMO_CAPEX)
    if cap is not None and len(cap):
        cap.columns = [c.lower() for c in cap.columns]
        iname = next((c for c in cap.columns if "industry" in c or "sector" in c), cap.columns[0])
        cap_c = next((c for c in cap.columns if "capex" in c or "capital expenditures" in c), cap.columns[-1])
        dep_c = next((c for c in cap.columns if "depr" in c or "amort" in c), None)
        for _,r in cap.iterrows():
            name = str(r.get(iname,"")).strip()
            if not name: continue
            cx = nfloat(r.get(cap_c)); dp = nfloat(r.get(dep_c))
            if isnum(cx) and cx>1.5: cx/=100.0
            if isnum(dp) and dp>1.5: dp/=100.0
            dmap.setdefault(name, {}).update({"capex_sales":cx, "dep_sales":dp})

    # NWC %
    wc = read_xlsx(DAMO_WC)
    if wc is not None and len(wc):
        wc.columns = [c.lower() for c in wc.columns]
        iname = next((c for c in wc.columns if "industry" in c or "sector" in c), wc.columns[0])
        nwc_c = next((c for c in wc.columns if "working capital" in c or "net working" in c or "nwc" in c), wc.columns[-1])
        for _,r in wc.iterrows():
            name = str(r.get(iname,"")).strip()
            if not name: continue
            v = nfloat(r.get(nwc_c)); v = (v/100.0 if isnum(v) and v>1.5 else v)
            dmap.setdefault(name, {}).update({"nwc_sales":v})

    # Betas
    ib = read_xlsx(DAMO_BETAS)
    if ib is not None and len(ib):
        ib.columns = [c.lower() for c in ib.columns]
        iname = next((c for c in ib.columns if "industry" in c or "sector" in c), ib.columns[0])
        b_c = next((c for c in ib.columns if "levered" in c and "beta" in c), None) or \
              next((c for c in ib.columns if "beta" in c), None)
        for _,r in ib.iterrows():
            name = str(r.get(iname,"")).strip()
            if not name: continue
            dmap.setdefault(name, {}).update({"beta": nfloat(r.get(b_c))})

    # Country defaults (US)
    rf, erp = 0.045, 0.043
    cr = read_xlsx(DAMO_COUNTRY)
    if cr is not None and len(cr):
        cr.columns = [c.lower() for c in cr.columns]
        ccol = next((c for c in cr.columns if "country" in c), cr.columns[0])
        rf_c = next((c for c in cr.columns if "risk free" in c or "government bond" in c), None)
        erp_c = next((c for c in cr.columns if "equity risk premium" in c or "erp" in c), None)
        us = cr[cr[ccol].astype(str).str.contains("united states|usa|^us$|u.s.", case=False, regex=True)]
        if len(us):
            rfv = nfloat(us.iloc[0].get(rf_c))
            erpv= nfloat(us.iloc[0].get(erp_c))
            rf  = rfv/100.0 if isnum(rfv) and rfv>1.5 else (rfv if isnum(rfv) else rf)
            erp = erpv/100.0 if isnum(erpv) and erpv>1.5 else (erpv if isnum(erpv) else erp)

    return sorted(industries), dmap, {"rf":rf, "erp":erp, "g_cap":0.02}

def fuzzy_defaults(industry: str, dmap: Dict[str,dict]) -> dict:
    if not dmap:
        return dict(op_margin=0.12, net_margin=0.10, dep_sales=0.05, capex_sales=0.05, nwc_sales=0.03, beta=1.0)
    ind = (industry or "").lower()
    pick = None
    for k in dmap:
        if ind in k.lower() or k.lower() in ind:
            pick = k; break
    if pick is None:
        toks = [t for t in ind.split() if t]
        for k in dmap:
            if any(t in k.lower() for t in toks):
                pick = k; break
    d = dmap.get(pick, {})
    def vget(key, dflt):
        v = nfloat(d.get(key))
        return dflt if not isnum(v) else v
    return dict(
        op_margin=vget("op_margin",0.12),
        net_margin=vget("net_margin",0.10),
        dep_sales=vget("dep_sales",0.05),
        capex_sales=vget("capex_sales",0.05),
        nwc_sales=vget("nwc_sales",0.03),
        beta=vget("beta",1.0)
    )

# ─────────────────────────────────────────────────────────────
# SEC CompanyFacts
# ─────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def fetch_companyfacts(cik: str) -> Optional[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json"
    r = requests.get(url, headers=SEC_HEADERS, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()

def _to_df(unit_list: List[dict]) -> pd.DataFrame:
    if not unit_list: return pd.DataFrame()
    df = pd.DataFrame(unit_list)
    for col in ("val","fy"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("end","start","filed"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.replace({"None": np.nan, "none": np.nan, "": np.nan})
    return df

def pick_annual(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df
    cand = df.copy()
    if "fp" in cand.columns:
        cand = cand[(cand["fp"]=="FY") | cand["fp"].isna()]
    if "form" in cand.columns:
        cand = cand[(cand["form"].isin(FY_FORMS)) | cand["form"].isna()]
    if "start" in cand.columns and "end" in cand.columns:
        dur = (cand["end"] - cand["start"]).dt.days
        cand = cand[(dur.isna()) | (dur.between(350, 375))]
    if "fy" in cand.columns and "filed" in cand.columns:
        cand = cand.sort_values(["fy","filed"]).drop_duplicates(subset=["fy"], keep="last")
    elif "fy" in cand.columns:
        cand = cand.sort_values("fy").drop_duplicates(subset=["fy"], keep="last")
    sort_col = "fy" if "fy" in cand.columns else "end"
    return cand.sort_values(sort_col, ascending=False)

def get_fact(sec: dict, tag: str, prefer_units=("USD",)) -> pd.DataFrame:
    facts = (sec or {}).get("facts", {}).get("us-gaap", {})
    if tag not in facts:
        return pd.DataFrame()
    units = facts[tag].get("units", {})
    for u in prefer_units:
        if u in units:
            return _to_df(units[u])
    for _, lst in units.items():
        if isinstance(lst, list):
            return _to_df(lst)
    return pd.DataFrame()

def build_history(sec: dict):
    TAGS = {
        "Revenue": ["Revenues","SalesRevenueNet","RevenueFromContractWithCustomerExcludingAssessedTax"],
        "OperatingIncome": ["OperatingIncomeLoss"],
        "NetIncome": ["NetIncomeLoss"],
        "DepAmort": ["DepreciationDepletionAndAmortization","DepreciationAndAmortization"],
        "CapEx": ["PaymentsToAcquirePropertyPlantAndEquipment","CapitalExpendituresIncurredButNotYetPaid"],
        "CFO": ["NetCashProvidedByUsedInOperatingActivities"],
        "InterestExp": ["InterestExpense"],
        "CurrentAssets": ["AssetsCurrent"],
        "CurrentLiabilities": ["LiabilitiesCurrent"],
    }
    series, used = {}, {}
    for key, tags in TAGS.items():
        merged = []
        for t in tags:
            df = get_fact(sec, t, ("USD",))
            if df.empty: continue
            merged.append(pick_annual(df))
        if merged:
            df = pd.concat(merged, ignore_index=True)
            df = df.sort_values(["fy","filed"]).drop_duplicates(subset=["fy"], keep="last")
            series[key] = df.sort_values("fy", ascending=False)
            used[key] = True
        else:
            series[key] = pd.DataFrame()
            used[key] = False

    base = None
    for k in ["Revenue","OperatingIncome","NetIncome","DepAmort","CapEx","CFO","InterestExp","CurrentAssets","CurrentLiabilities"]:
        df = series.get(k, pd.DataFrame())
        if df.empty: continue
        df2 = df[["fy","val"]].copy().rename(columns={"val":k})
        base = df2 if base is None else base.merge(df2, on="fy", how="outer")
    if base is None:
        base = pd.DataFrame(columns=["fy"])

    base = base.sort_values("fy", ascending=False).reset_index(drop=True)
    base = base.replace({"None": np.nan, "none": np.nan, "": np.nan})
    base = to_numeric_df(base, except_cols=("fy",))
    if "CapEx" in base.columns:
        base["CapEx"] = base["CapEx"].abs()

    def latest_val_of(tags: List[str], units=("USD",)) -> Optional[float]:
        for t in tags:
            df = get_fact(sec, t, units)
            if df.empty: continue
            dfa = pick_annual(df)
            s = pd.to_numeric(dfa.sort_values("fy", ascending=False)["val"], errors="coerce").dropna()
            if len(s): return float(s.iloc[0])
        return None

    cash = latest_val_of(["CashAndCashEquivalentsAtCarryingValue"])
    long_debt = latest_val_of(["LongTermDebtNoncurrent","LongTermDebt"])
    short_debt= latest_val_of(["ShortTermBorrowings","DebtCurrent"])
    total_debt = (long_debt or 0.0) + (short_debt or 0.0) if (long_debt is not None or short_debt is not None) else None

    def latest_shares():
        for tag in ["WeightedAverageNumberOfDilutedSharesOutstanding","CommonStockSharesOutstanding"]:
            d = get_fact(sec, tag, ("shares",))
            if d.empty: continue
            dd = pick_annual(d)
            s = pd.to_numeric(dd.sort_values("fy", ascending=False)["val"], errors="coerce").dropna()
            if len(s): return float(s.iloc[0])
        return None

    shares = latest_shares()

    latest = {
        "cash": cash,
        "total_debt": total_debt,
        "net_debt": (total_debt or 0.0) - (cash or 0.0),
        "shares_diluted": shares
    }
    return base, latest, used

# ─────────────────────────────────────────────────────────────
# Finance (WACC, DCF)
# ─────────────────────────────────────────────────────────────
def compute_wacc(rf, erp, beta, kd, debt_wt, tax_rate):
    ke = float(rf) + float(beta) * float(erp)
    we = 1.0 - float(debt_wt)
    wacc = we * ke + float(debt_wt) * float(kd) * (1 - float(tax_rate))
    return float(max(wacc, 0.0001)), float(ke), float(kd)

def run_fcff_dcf(revenue, op_margin, dep_pct, capex_pct, nwc_pct,
                 growth, years, terminal_g, tax_rate, wacc, shares, net_debt):
    wacc = float(max(wacc, float(terminal_g) + EPS))
    shares = float(max(nfloat(shares) or 0.0, 1.0))
    net_debt = float(nfloat(net_debt) or 0.0)
    revenue = float(max(nfloat(revenue) or 0.0, 1.0))

    rows = []
    sales = revenue
    prev_wc = sales * float(nwc_pct)
    for t in range(1, int(years)+1):
        sales *= (1 + float(growth))
        ebit  = sales * float(op_margin)
        nopat = ebit * (1 - float(tax_rate))
        dep   = sales * float(dep_pct)
        capex = sales * float(capex_pct)
        wc    = sales * float(nwc_pct)
        dNWC  = wc - prev_wc
        fcff  = nopat + dep - capex - dNWC
        pv    = fcff / ((1 + wacc) ** t)
        rows.append([t, sales, ebit, nopat, dep, capex, dNWC, fcff, pv])
        prev_wc = wc

    df = pd.DataFrame(rows, columns=["Year","Sales","EBIT","NOPAT","Dep","CapEx","ΔNWC","FCFF","PV(FCFF)"])
    tv = df["FCFF"].iloc[-1] * (1 + float(terminal_g)) / max(wacc - float(terminal_g), EPS)
    pv_tv = tv / ((1 + wacc) ** years)
    ev = df["PV(FCFF)"].sum() + pv_tv
    equity = ev - net_debt
    price = equity / shares
    return df, tv, pv_tv, ev, equity, price

def run_equity_fcfe(revenue, net_margin, dep_pct, capex_pct, nwc_pct,
                    growth, years, terminal_g, ke, shares):
    ke = float(max(ke, float(terminal_g) + EPS))
    shares = float(max(nfloat(shares) or 0.0, 1.0))
    revenue = float(max(nfloat(revenue) or 0.0, 1.0))

    rows = []
    sales = revenue
    prev_wc = sales * float(nwc_pct)
    for t in range(1, int(years)+1):
        sales *= (1 + float(growth))
        ni    = sales * float(net_margin)
        dep   = sales * float(dep_pct)
        capex = sales * float(capex_pct)
        wc    = sales * float(nwc_pct)
        dNWC  = wc - prev_wc
        fcfe  = ni + dep - capex - dNWC
        pv    = fcfe / ((1 + ke) ** t)
        rows.append([t, sales, ni, dep, capex, dNWC, fcfe, pv])
        prev_wc = wc

    df = pd.DataFrame(rows, columns=["Year","Sales","NI","Dep","CapEx","ΔNWC","FCFE","PV(FCFE)"])
    tv = df["FCFE"].iloc[-1] * (1 + float(terminal_g)) / max(ke - float(terminal_g), EPS)
    pv_tv = tv / ((1 + ke) ** years)
    equity = df["PV(FCFE)"].sum() + pv_tv
    price = equity / shares
    return df, tv, pv_tv, equity, price

# Alpha-like table (WITH Year 0 values filled)
def alpha_table(model: str, inputs: dict, ke: float) -> Tuple[pd.DataFrame, float, float, float, float, float]:
    if model in ("equity_ni", "equity_fcfe"):
        df, tv, pv_tv, eq, price = run_equity_fcfe(
            revenue=inputs["revenue"],
            net_margin=inputs["net_margin"],
            dep_pct=inputs["dep_pct"],
            capex_pct=inputs["capex_pct"],
            nwc_pct=inputs["nwc_pct"],
            growth=inputs["growth"],
            years=inputs["years"],
            terminal_g=inputs["terminal_g"],
            ke=ke,
            shares=inputs["shares"]
        )
        yrs = ["Year 0"] + [f"Year {int(y)}" for y in df["Year"]] + ["Terminal"]
        rev0      = inputs["revenue"]
        margin0   = inputs["net_margin"]
        ni0       = rev0 * margin0
        dep0      = rev0 * inputs["dep_pct"]
        capex0    = rev0 * inputs["capex_pct"]
        dnwk0     = 0.0
        fcfe0     = ni0 + dep0 - capex0 - dnwk0

        rev      = [rev0] + list(df["Sales"]) + [np.nan]
        margin   = [margin0] + [inputs["net_margin"]]*len(df) + [np.nan]
        ni       = [ni0] + list(df["NI"]) + [np.nan]
        dep      = [dep0] + list(df["Dep"]) + [np.nan]
        capex    = [capex0] + list(df["CapEx"]) + [np.nan]
        dnwk     = [dnwk0] + list(df["ΔNWC"]) + [np.nan]
        fcfe     = [fcfe0] + list(df["FCFE"]) + [tv]
        pv_fcfe  = [np.nan] + list(df["PV(FCFE)"]) + [pv_tv]

        table = pd.DataFrame(
            [
                ["Revenue"] + rev,
                ["Net margin"] + margin,
                ["Net Income"] + ni,
                ["Dep"] + dep,
                ["CapEx"] + capex,
                ["ΔNWC"] + dnwk,
                ["FCFE"] + fcfe,
                ["Discount Rate"] + [ke]* (len(yrs)-1),
                ["Present Value"] + pv_fcfe,
            ],
            columns=["Item"] + yrs
        ).set_index("Item")

        fmt = table.copy()
        for c in yrs:
            def fmt_cell(val, row):
                if pd.isna(val): return "—"
                if row in ["Net margin","Discount Rate"]:
                    return f"{float(val):.2%}"
                if row in ["Revenue","Net Income","Dep","CapEx","ΔNWC","FCFE","Present Value"]:
                    return human(val)
                return f"{val:,.2f}"
            fmt[c] = [fmt_cell(fmt.loc[r, c], r) for r in fmt.index]
        return fmt, tv, pv_tv, np.nan, eq, price

    # Firm FCFF (+ no-CapEx option)
    capex_pct = inputs["capex_pct"]
    if model == "firm_fcff_nocapex":
        capex_pct = 0.0
    df, tv, pv_tv, ev, eq, price = run_fcff_dcf(
        revenue=inputs["revenue"], op_margin=inputs["op_margin"],
        dep_pct=inputs["dep_pct"], capex_pct=capex_pct, nwc_pct=inputs["nwc_pct"],
        growth=inputs["growth"], years=inputs["years"], terminal_g=inputs["terminal_g"],
        tax_rate=inputs["tax_rate"], wacc=inputs["wacc"], shares=inputs["shares"],
        net_debt=inputs["net_debt"]
    )
    yrs = ["Year 0"] + [f"Year {int(y)}" for y in df["Year"]] + ["Terminal"]
    rev0    = inputs["revenue"]
    marg0   = inputs["op_margin"]
    ebit0   = rev0 * marg0
    taxes0  = - ebit0 * inputs["tax_rate"]
    nopat0  = ebit0 * (1 - inputs["tax_rate"])
    dep0    = rev0 * inputs["dep_pct"]
    capex0  = rev0 * capex_pct
    dnwk0   = 0.0
    fcff0   = nopat0 + dep0 - capex0 - dnwk0

    rev      = [rev0] + list(df["Sales"]) + [np.nan]
    margin   = [marg0] + [inputs["op_margin"]]*len(df) + [np.nan]
    ebit     = [ebit0] + list(df["EBIT"]) + [np.nan]
    taxes    = [taxes0] + list(-(df["EBIT"]*inputs["tax_rate"])) + [np.nan]
    nopat    = [nopat0] + list(df["NOPAT"]) + [np.nan]
    dep      = [dep0] + list(df["Dep"]) + [np.nan]
    capex    = [capex0] + list(df["CapEx"]) + [np.nan]
    dnwk     = [dnwk0] + list(df["ΔNWC"]) + [np.nan]
    fcff     = [fcff0] + list(df["FCFF"]) + [tv]
    pv_fcff  = [np.nan] + list(df["PV(FCFF)"]) + [pv_tv]

    table = pd.DataFrame(
        [
            ["Revenue"] + rev,
            ["Operating Margin"] + margin,
            ["Operating Income"] + ebit,
            ["Taxes"] + taxes,
            ["NOPAT"] + nopat,
            ["Dep"] + dep,
            ["CapEx"] + capex,
            ["ΔNWC"] + dnwk,
            ["FCFF"] + fcff,
            ["Discount Rate"] + [inputs["wacc"]] * (len(yrs)-1),
            ["Present Value"] + pv_fcff,
        ],
        columns=["Item"] + yrs
    ).set_index("Item")

    fmt = table.copy()
    for c in yrs:
        def fmt_cell(val, row):
            if pd.isna(val): return "—"
            if row in ["Operating Margin","Discount Rate"]:
                return f"{float(val):.2%}"
            if row in ["Revenue","Operating Income","Taxes","NOPAT","Dep","CapEx","ΔNWC","FCFF","Present Value"]:
                return human(val)
            return f"{val:,.2f}"
        fmt[c] = [fmt_cell(fmt.loc[r, c], r) for r in fmt.index]
    return fmt, tv, pv_tv, ev, eq, price

# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────
st.markdown("## 📊 DCF Valuation (SEC XBRL + Damodaran)")
st.caption("Free SEC CompanyFacts (no API key). Build FCFF/WACC by the book. Country fixed: USA.")

# Lists
try:
    tickers = load_tickers()
except Exception as e:
    st.error(f"Could not load tickers: {e}")
    st.stop()

industries, ind_map, us_defaults = load_damodaran()

c1, c2 = st.columns([2, 2])
with c1:
    sel_label = st.selectbox("Company", tickers["label"].tolist(), index=None, placeholder="Type to search…")
with c2:
    sel_ind = st.selectbox("Industry (Damodaran)", industries if industries else ["(defaults)"], index=0)

if not sel_label:
    st.info("Pick a company to continue.")
    st.stop()

row = tickers[tickers["label"] == sel_label].iloc[0]
ticker = row["ticker"]; company_name = row["title"]; cik = str(row["cik_str"]).zfill(10)
st.markdown(f"### {company_name} ({ticker})")

with st.spinner("Fetching SEC XBRL CompanyFacts…"):
    sec = fetch_companyfacts(cik)
if sec is None:
    st.error("SEC CompanyFacts fetch failed (network or rate-limit). Try again.")
    st.stop()

hist, latest, used_tags = build_history(sec)
histN = hist.head(5).copy()
if histN.empty or "fy" not in histN.columns:
    st.error("Could not find annual series in SEC facts for this issuer.")
    st.stop()

# Market snapshot (robust Market Cap)
last_price, mkt_cap = np.nan, np.nan
shares_yf = np.nan
shares_sec = nfloat(latest.get("shares_diluted"))

if yf is not None:
    try:
        y = yf.Ticker(ticker)
        h = y.history(period="5d")
        if not h.empty:
            last_price = float(h["Close"].iloc[-1])
        info = getattr(y, "fast_info", {}) or {}
        if "market_cap" in info and info["market_cap"]:
            mkt_cap = float(info["market_cap"])
        if "shares_outstanding" in info and info["shares_outstanding"]:
            shares_yf = float(info["shares_outstanding"])
    except Exception:
        pass

shares_for_mc = nfloat(shares_sec) or nfloat(shares_yf)
if not isnum(mkt_cap) and isnum(last_price) and isnum(shares_for_mc):
    mkt_cap = float(last_price) * float(shares_for_mc)
if not isnum(last_price) and isnum(mkt_cap) and isnum(shares_for_mc) and float(shares_for_mc) > 0:
    last_price = float(mkt_cap) / float(shares_for_mc)

shares_est = float(nfloat(shares_sec) or nfloat(shares_yf) or np.nan)

mc = st.columns(5)
mc[0].metric("Last Price", human(last_price,2))
mc[1].metric("Market Cap", human(mkt_cap))
mc[2].metric("Shares (diluted est.)", human(shares_est,2,prefix=""))
mc[3].metric("Cash (SEC)", human(latest.get("cash")))
mc[4].metric("Total Debt (SEC)", human(latest.get("total_debt")))

# Historical (pretty)
st.markdown("### Historical (SEC XBRL) — annual")
hist_show = histN.copy()
for c in hist_show.columns:
    if c == "fy":
        continue
    hist_show[c] = hist_show[c].map(human_inline)
st.dataframe(hist_show.rename(columns={"fy":"FY"}).set_index("FY"), use_container_width=True)

# Defaults & sources
defaults = fuzzy_defaults(sel_ind, ind_map)
rf, erp, g_cap = us_defaults["rf"], us_defaults["erp"], us_defaults["g_cap"]

last_row = histN.iloc[0]
rev_last   = nfloat(last_row.get("Revenue"))
ebit_last  = nfloat(last_row.get("OperatingIncome"))
ni_last    = nfloat(last_row.get("NetIncome"))

op_margin_from_sec  = isnum(rev_last) and isnum(ebit_last) and rev_last>0
net_margin_from_sec = isnum(rev_last) and isnum(ni_last)  and rev_last>0

op_margin_pref  = clamp(ebit_last/rev_last, 0.03, 0.60) if op_margin_from_sec  else nfloat(defaults["op_margin"])
net_margin_pref = clamp(ni_last/rev_last,  0.00, 0.50) if net_margin_from_sec else nfloat(defaults["net_margin"])

dep_last = nfloat(last_row.get("DepAmort"))
dep_from_sec = isnum(dep_last) and isnum(rev_last) and rev_last>0
dep_pct_pref = clamp(dep_last/rev_last, 0.00, 0.20) if dep_from_sec else nfloat(defaults["dep_sales"])

cap_last = nfloat(last_row.get("CapEx"))
capex_from_sec = isnum(cap_last) and isnum(rev_last) and rev_last>0
capex_pct_pref = clamp(cap_last/rev_last, 0.00, 0.25) if capex_from_sec else nfloat(defaults["capex_sales"])

nwc_pct_pref = nfloat(defaults["nwc_sales"]) if isnum(defaults["nwc_sales"]) else 0.03

# Growth guess
try:
    if len(histN) >= 2 and isnum(histN["Revenue"].iloc[1]) and histN["Revenue"].iloc[1] > 0:
        yrs_gap = (histN["fy"].iloc[0] - histN["fy"].iloc[1]) or 1
        cagr = (histN["Revenue"].iloc[0] / histN["Revenue"].iloc[1]) ** (1/yrs_gap) - 1
    else:
        cagr = 0.05
    growth_pref = clamp(cagr, 0.00, 0.20)
except Exception:
    growth_pref = 0.05

# Cost of debt
kd_pref = 0.065
interest = nfloat(last_row.get("InterestExp"))
debt_avg = nfloat(latest.get("total_debt"))
if isnum(interest) and isnum(debt_avg) and debt_avg>0:
    kd_pref = clamp(abs(interest)/debt_avg, 0.02, 0.12)

shares_source = "SEC" if isnum(shares_sec) else ("Yahoo" if isnum(shares_yf) else "User")
shares_pref = float(nfloat(shares_sec) or nfloat(shares_yf) or 1_000_000_000.0)

net_debt_from_sec = isnum(latest.get("cash")) or isnum(latest.get("total_debt"))
net_debt_pref = float(nfloat(latest.get("net_debt")) or 0.0)

# ─────────────────────────────────────────────────────────────
# Inputs (NO operating-model selector here)
# ─────────────────────────────────────────────────────────────
st.markdown("### Inputs & Assumptions (edit as needed)")
c1a,c2a,c3a,c4a = st.columns(4)
with c1a:
    revenue = st.number_input("Latest Revenue (USD)", min_value=0.0, value=float(rev_last if isnum(rev_last) else 1_000_000_000.0), step=1e6, format="%.0f")
with c2a:
    shares  = st.number_input("Diluted Shares Outstanding", min_value=1.0, value=shares_pref, step=1e6, format="%.0f")
with c3a:
    net_debt= st.number_input("Net Debt (Debt − Cash)", value=net_debt_pref, step=1e6, format="%.0f")
with c4a:
    tax_rate= st.slider("Tax Rate", 0.00, 0.40, 0.21, 0.005)

c5,c6,c7,c8 = st.columns(4)
with c5: op_margin  = st.slider("Operating Margin (EBIT/Sales)", 0.00, 0.60, float(op_margin_pref if isnum(op_margin_pref) else 0.12), 0.005)
with c6: net_margin = st.slider("Net Margin (NI/Sales)",        0.00, 0.50, float(net_margin_pref if isnum(net_margin_pref) else 0.10), 0.005)
with c7: dep_pct    = st.slider("Depreciation as % of Sales",   0.00, 0.20, float(dep_pct_pref if isnum(dep_pct_pref) else 0.05), 0.005)
with c8: capex_pct  = st.slider("CapEx as % of Sales",          0.00, 0.25, float(capex_pct_pref if isnum(capex_pct_pref) else 0.05), 0.005)

c9,c10,c11 = st.columns(3)
with c9:  years      = st.slider("Forecast Horizon (years)", 3, 10, 5, 1)
with c10: growth     = st.slider("Revenue Growth (Y1–Y5)", 0.00, 0.20, float(growth_pref), 0.005)
with c11: terminal_g = st.slider("Terminal Growth (≤ long-run cap)", 0.00, float(us_defaults["g_cap"]), min(0.02, us_defaults["g_cap"]), 0.001)

c12,c13,c14 = st.columns(3)
with c12: beta    = st.slider("Levered Beta (industry)", 0.40, 2.50, float(fuzzy_defaults(sel_ind, ind_map)["beta"]), 0.01)
with c13: debt_wt = st.slider("Debt Weight (WACC)", 0.0, 0.70, 0.20, 0.01)
with c14: kd      = st.slider("Pretax Cost of Debt", 0.01, 0.15, float(kd_pref), 0.001)

wacc, ke, kd_eff = compute_wacc(us_defaults["rf"], us_defaults["erp"], beta, kd, debt_wt, tax_rate)
st.caption(f"WACC **{wacc:.2%}** • Ke **{ke:.2%}**  |  rf {us_defaults['rf']:.2%}, ERP {us_defaults['erp']:.2%}, β {beta:.2f}")

# Assumptions & Sources (single explicit source)
ass_rows = [
    ("Revenue (last FY)", revenue, "SEC", "us-gaap:Revenues / SalesRevenueNet"),
    ("Operating margin", op_margin, ("SEC" if op_margin_from_sec else "Damodaran"), "us-gaap:OperatingIncomeLoss"),
    ("Net margin", net_margin, ("SEC" if net_margin_from_sec else "Damodaran"), "us-gaap:NetIncomeLoss"),
    ("Depreciation / Sales", dep_pct, ("SEC" if dep_from_sec else "Damodaran"), "us-gaap:DepreciationAndAmortization"),
    ("CapEx / Sales", capex_pct, ("SEC" if capex_from_sec else "Damodaran"), "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"),
    ("NWC / Sales", nwc_pct_pref, "Damodaran", "Damodaran WC % of Sales"),
    ("Tax rate", tax_rate, "User", ""),
    ("β (levered)", beta, "Damodaran",""),
    ("Debt weight", debt_wt, "User",""),
    ("rf (US)", us_defaults["rf"], "Damodaran",""),
    ("ERP (US)", us_defaults["erp"], "Damodaran",""),
    ("Terminal g", terminal_g, "User (capped)",""),
    ("Net Debt", net_debt, ("SEC" if net_debt_from_sec else "User"), "Cash, Long/Short Debt"),
    ("Shares (diluted)", shares, shares_source, "WeightedAverageNumberOfDilutedSharesOutstanding"),
]
st.markdown("### Assumptions & Sources")
st.dataframe(pd.DataFrame(ass_rows, columns=["Item","Value","Source","Tag/Reference"]), use_container_width=True)

# Benchmarks (with Source)
st.markdown("### Benchmarks vs Industry")
company_sources = {
    "EBIT margin": "SEC" if op_margin_from_sec else "Damodaran",
    "Net margin": "SEC" if net_margin_from_sec else "Damodaran",
    "Dep / Sales": "SEC" if dep_from_sec else "Damodaran",
    "CapEx / Sales":"SEC" if capex_from_sec else "Damodaran",
    "NWC / Sales":"Damodaran",
    "β (levered)":"Damodaran",
}
bench = pd.DataFrame(
    [
        ("EBIT margin", op_margin,  defaults["op_margin"],  company_sources["EBIT margin"]),
        ("Net margin",  net_margin, defaults["net_margin"], company_sources["Net margin"]),
        ("Dep / Sales", dep_pct,    defaults["dep_sales"],  company_sources["Dep / Sales"]),
        ("CapEx / Sales", capex_pct,defaults["capex_sales"],company_sources["CapEx / Sales"]),
        ("NWC / Sales",  nwc_pct_pref, defaults["nwc_sales"], company_sources["NWC / Sales"]),
        ("β (levered)",  beta,      defaults["beta"],       company_sources["β (levered)"]),
    ],
    columns=["Metric","Company","Industry","Source"]
).set_index("Metric")
st.dataframe(bench.style.format({"Company":"{:.1%}","Industry":"{:.1%}"}), use_container_width=True)

st.divider()

# ─────────────────────────────────────────────────────────────
# Run button (always 3-scenario), then choose model to VIEW
# ─────────────────────────────────────────────────────────────
if "ran" not in st.session_state:
    st.session_state.ran = False

base_inputs = dict(
    revenue=revenue, op_margin=op_margin, net_margin=net_margin,
    dep_pct=dep_pct, capex_pct=capex_pct, nwc_pct=nwc_pct_pref,
    growth=growth, years=years, terminal_g=terminal_g, tax_rate=tax_rate,
    wacc=wacc, shares=shares, net_debt=net_debt
)

def show_results_alpha(model: str, inputs: dict, scenario_label: str):
    tbl, tv, pv_tv, ev, eq, price = alpha_table(model, inputs, ke)

    model_label_map = {
        "equity_ni": "Equity Model: via Net Income",
        "equity_fcfe": "Equity Model: via FCFE",
        "firm_fcff": "Firm Model: via FCFF",
        "firm_fcff_nocapex": "Firm Model: via FCFF, w/o CapEx",
    }
    # >>> change 1: include scenario label in the title
    st.markdown(f"### Operating Table — **{scenario_label}** — {model_label_map[model]}")
    st.caption("Currency: USD • Figures formatted")
    st.dataframe(tbl, use_container_width=True)

    # >>> change 2: fix formula text (build bullets line-by-line)
    lines = [f"- PV of Terminal Value: **{human(pv_tv)}**"]
    if isnum(ev):
        lines.append(f"- Enterprise Value (EV): **{human(ev)}**")
    lines.append(f"- Equity Value: **{human(eq)}**")
    lines.append(f"- Implied Price / Share: **{human(price,2)}**")
    st.markdown("\n".join(lines))

    if isnum(last_price):
        diff = float(price) - float(last_price)
        pct  = diff/float(last_price) if last_price else np.nan
        st.metric("Upside vs Last Price", f"{pct*100:,.1f}%", delta=f"{diff:,.2f}")
        st.caption(f"Last Price: {human(last_price,2)} • DCF Price: {human(price,2)}")

    # Sensitivities colored vs last price
    st.markdown("### Sensitivity Analysis")
    def color_cond():
        lp = float(last_price) if isnum(last_price) else 0.0
        return alt.condition(alt.datum.Price > lp, alt.value("#16a34a"), alt.value("#dc2626"))

    tg, tm, tw = st.tabs(["Revenue Growth", "Margin", "Discount Rate"])

    with tg:
        g_vals = np.round(np.linspace(0.00, 0.50, 51), 4)
        prices = [alpha_table(model, {**inputs,"growth":float(g)}, ke)[5] for g in g_vals]
        chart_df = pd.DataFrame({"Growth": g_vals, "Price": prices})
        st.altair_chart(
            alt.Chart(chart_df).mark_bar().encode(
                x=alt.X("Growth:Q", axis=alt.Axis(format='%')),
                y=alt.Y("Price:Q"),
                color=color_cond()
            ).properties(height=320),
            use_container_width=True
        )

    with tm:
        if model in ("equity_ni","equity_fcfe"):
            m_vals = np.round(np.linspace(0.03, 0.50, 48), 4)
            prices = [alpha_table(model, {**inputs,"net_margin":float(m)}, ke)[5] for m in m_vals]
            chart_df = pd.DataFrame({"NetMargin": m_vals, "Price": prices})
            st.altair_chart(
                alt.Chart(chart_df).mark_bar().encode(
                    x=alt.X("NetMargin:Q", axis=alt.Axis(format='%')),
                    y=alt.Y("Price:Q"),
                    color=color_cond()
                ).properties(height=320),
                use_container_width=True
            )
        else:
            m_vals = np.round(np.linspace(0.03, 0.60, 58), 4)
            prices = [alpha_table(model, {**inputs,"op_margin":float(m)}, ke)[5] for m in m_vals]
            chart_df = pd.DataFrame({"Margin": m_vals, "Price": prices})
            st.altair_chart(
                alt.Chart(chart_df).mark_bar().encode(
                    x=alt.X("Margin:Q", axis=alt.Axis(format='%')),
                    y=alt.Y("Price:Q"),
                    color=color_cond()
                ).properties(height=320),
                use_container_width=True
            )

    with tw:
        if model in ("equity_ni","equity_fcfe"):
            d_vals = np.round(np.linspace(max(0.05, ke-0.05), min(0.20, ke+0.05), 51), 4)
            prices = [alpha_table(model, inputs, d)[5] for d in d_vals]
        else:
            d_vals = np.round(np.linspace(max(0.05, inputs["wacc"]-0.05), min(0.20, inputs["wacc"]+0.05), 51), 4)
            prices = [alpha_table(model, {**inputs,"wacc":float(w)}, ke)[5] for w in d_vals]
        chart_df = pd.DataFrame({"Rate": d_vals, "Price": prices})
        st.altair_chart(
            alt.Chart(chart_df).mark_bar().encode(
                x=alt.X("Rate:Q", axis=alt.Axis(format='%')),
                y=alt.Y("Price:Q"),
                color=color_cond()
            ).properties(height=320),
            use_container_width=True
        )

    # Forecast lines
    st.markdown("### DCF Financials (Forecast)")
    if model in ("equity_ni","equity_fcfe"):
        df, *_ = run_equity_fcfe(inputs["revenue"], inputs["net_margin"], inputs["dep_pct"], inputs["capex_pct"], inputs["nwc_pct"], inputs["growth"], inputs["years"], inputs["terminal_g"], ke, inputs["shares"])
        ch = (pd.DataFrame({
            "Year": df["Year"],
            "Revenue (B)": df["Sales"]/1e9,
            "NI (B)": df["NI"]/1e9,
            "FCFE (B)": df["FCFE"]/1e9
        }).melt("Year", var_name="Series", value_name="USD (Billions)"))
    else:
        df, *_ = run_fcff_dcf(inputs["revenue"], inputs["op_margin"], inputs["dep_pct"], (0.0 if model=="firm_fcff_nocapex" else inputs["capex_pct"]), inputs["nwc_pct"], inputs["growth"], inputs["years"], inputs["terminal_g"], inputs["tax_rate"], inputs["wacc"], inputs["shares"], inputs["net_debt"])
        ch = (pd.DataFrame({
            "Year": df["Year"],
            "Revenue (B)": df["Sales"]/1e9,
            "NOPAT (B)": df["NOPAT"]/1e9,
            "FCFF (B)": df["FCFF"]/1e9
        }).melt("Year", var_name="Series", value_name="USD (Billions)"))
    st.altair_chart(
        alt.Chart(ch).mark_line(point=True).encode(
            x="Year:O", y="USD (Billions):Q", color="Series:N"
        ).properties(height=320),
        use_container_width=True
    )

# Run 3-scenario
if st.button("🚀 Run 3-Scenario DCF"):
    st.session_state.ran = True

if st.session_state.ran:
    # Model selector appears AFTER running
    model_label = st.selectbox(
        "View Operating Model",
        [
            "Equity Model: via Net Income",
            "Equity Model: via FCFE",
            "Firm Model: via FCFF",
            "Firm Model: via FCFF, w/o CapEx",
        ],
        index=2
    )
    MODEL_MAP = {
        "Equity Model: via Net Income":"equity_ni",
        "Equity Model: via FCFE":"equity_fcfe",
        "Firm Model: via FCFF":"firm_fcff",
        "Firm Model: via FCFF, w/o CapEx":"firm_fcff_nocapex",
    }
    model = MODEL_MAP[model_label]

    cons = {**base_inputs, "growth":max(0.00, base_inputs["growth"]-0.02)}
    base = {**base_inputs}
    opt  = {**base_inputs, "growth":min(0.20, base_inputs["growth"]+0.02)}

    summary = []
    for tag, inp in [("Conservative", cons), ("Base", base), ("Optimistic", opt)]:
        _tbl, _tv, _pvtv, _ev, _eq, _pps = alpha_table(model, inp, ke)
        summary.append((tag, _pps, _ev, _eq))
    st.markdown("### Scenario Summary (per share)")
    st.dataframe(
        pd.DataFrame(summary, columns=["Scenario","Implied Price","EV","Equity"]).assign(
            **{"Implied Price": lambda d: d["Implied Price"].map(lambda v: human(v,2))},
            EV=lambda d: d["EV"].map(human), Equity=lambda d: d["Equity"].map(human)
        ),
        use_container_width=True
    )

    show_results_alpha(model, cons, "Conservative")
    show_results_alpha(model, base, "Base")
    show_results_alpha(model, opt, "Optimistic")

st.markdown("---")
st.markdown(
"""
**Methods:**  
• **Firm (FCFF):** FCFF = EBIT×(1−Tax) + Dep − CapEx − ΔNWC; discount at **WACC**; Equity = EV − Net Debt.  
• **Equity (FCFE / via NI):** FCFE = Net Income + Dep − CapEx − ΔNWC; discount at **Ke**; price = Equity / Shares.  
• **Terminal value:** Gordon with g < discount rate (clamped by long-run growth).  
**Sources:** SEC CompanyFacts (XBRL) for facts; Damodaran industry for benchmarks; Yahoo price optional.  
"""
)
