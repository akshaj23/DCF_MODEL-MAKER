DCF Valuation (SEC XBRL + Damodaran)

A lightweight Streamlit app that builds a discounted cash flow (DCF) from SEC CompanyFacts (XBRL) and uses Prof. Aswath Damodaran’s “Current Data” spreadsheets for conservative, industry-level defaults (margins, betas, CapEx/Dep, working capital, and country risk premiums). Yahoo Finance is optionally used for a quick market snapshot.

What you need to download (Damodaran data)

Save these files into the Data/ folder with the exact filenames listed on the left:

Operating&NetMarginsbyIndustry(US).xlsx
https://pages.stern.nyu.edu/~adamodar/pc/datasets/margin.xls

CapitalExpendituresbySector(US).xlsx
https://pages.stern.nyu.edu/~adamodar/pc/datasets/capex.xls

WorkingCapitalComponentsPercentofSales.xlsx
https://pages.stern.nyu.edu/~adamodar/pc/datasets/wcdata.xls

Industry-Betas.xlsx
https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls

CountryRiskPremiums.xlsx
https://www.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xls

(Optional) EnterpriseValueMultiplesbySector.xlsx
https://pages.stern.nyu.edu/~adamodar/pc/datasets/vebitda.xls

Notes:

Damodaran’s originals are often .xls. You can open and re-save as .xlsx with the target names above, or update the constants in app.py to point at the downloaded filenames directly.

Data is refreshed annually (usually January). Replace the files when new versions are posted.

Folder layout
.
├─ app.py
├─ requirements.txt
└─ Data/
   ├─ Operating&NetMarginsbyIndustry(US).xlsx
   ├─ CapitalExpendituresbySector(US).xlsx
   ├─ WorkingCapitalComponentsPercentofSales.xlsx
   ├─ Industry-Betas.xlsx
   ├─ CountryRiskPremiums.xlsx
   └─ (optional) EnterpriseValueMultiplesbySector.xlsx

Data sources

SEC CompanyFacts (XBRL) via public API (revenue, EBIT, net income, depreciation & amortization, CapEx, operating cash flow, interest, current assets/liabilities, cash, debt, diluted shares).

Damodaran “Current Data” (industry benchmarks, betas, and country risk).

Yahoo Finance (optional) for price/market cap.

How defaults are picked (high level)

If available from SEC: use 3-year trailing ratios (e.g., EBIT/Sales, Dep/Sales, CapEx/Sales, NWC/Sales).

If incomplete: fall back to the latest FY ratio.

If still missing: fall back to Damodaran’s industry averages.

US risk-free rate and ERP come from Damodaran’s country sheet; levered beta from industry betas.

Users can edit any assumption via sliders; WACC/Ke are derived from the inputs.

Attribution

All third-party data © their respective owners.
Industry and risk datasets © Prof. Aswath Damodaran (NYU Stern).
This project is for educational purposes only—no investment advice.
