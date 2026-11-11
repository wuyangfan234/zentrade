/**
 * 宏观日历事件名的中文对照 (后端没给 title_zh 时的兜底)。
 * 只收常见的固定事件与两类模板 (官员讲话 / 国债拍卖); 没有对照的保留英文原名, 原名始终在悬停里。
 */
const FIXED: Record<string, string> = {
  'NFIB Small Business Index': 'NFIB 小企业信心指数',
  'ADP Weekly Employment Change': 'ADP 周度就业变动',
  'ADP Non-Farm Employment Change': 'ADP 非农就业变动',
  'API Weekly Statistical Bulletin': 'API 周度原油库存',
  'Mortgage Delinquencies': '房贷拖欠率',
  'Federal Budget Balance': '联邦预算收支',
  'FOMC Meeting Minutes': 'FOMC 会议纪要',
  'FOMC Statement': 'FOMC 声明',
  'FOMC Press Conference': 'FOMC 新闻发布会',
  'FOMC Economic Projections': 'FOMC 经济预测',
  'Federal Funds Rate': '联邦基金利率',
  'Beige Book': '褐皮书',
  'Bank Holiday': '银行假日',
  'Natural Gas Storage': '天然气库存',
  'Crude Oil Inventories': 'EIA 原油库存',
  'Consumer Credit m/m': '消费信贷环比',
  'Wholesale Inventories m/m': '批发库存环比',
  'Business Inventories m/m': '企业库存环比',
  'Import Prices m/m': '进口价格环比',
  'Industrial Production m/m': '工业产出环比',
  'Capacity Utilization Rate': '产能利用率',
  'TIC Long-Term Purchases': 'TIC 长期资本净流入',
  'Treasury Currency Report': '财政部汇率报告',
}

const TEMPLATES: [RegExp, (m: RegExpExecArray) => string][] = [
  [/^Fed Chair (\w[\w.'-]*) (Speaks|Testifies)$/, (m) => `美联储主席 ${m[1]} ${m[2] === 'Speaks' ? '讲话' : '作证'}`],
  [/^FOMC Member (\w[\w.'-]*) Speaks$/, (m) => `美联储 ${m[1]} 讲话`],
  [/^Treasury Sec (\w[\w.'-]*) Speaks$/, (m) => `财长 ${m[1]} 讲话`],
  [/^President (\w[\w.'-]*) Speaks$/, (m) => `总统 ${m[1]} 讲话`],
  [/^(\d+)-y Bond Auction$/, (m) => `${m[1]} 年期国债拍卖`],
]

export function eventTitleZh(title: string): string {
  const t = (title ?? '').trim()
  if (FIXED[t]) return FIXED[t]
  for (const [re, fn] of TEMPLATES) {
    const m = re.exec(t)
    if (m) return fn(m)
  }
  return title
}
