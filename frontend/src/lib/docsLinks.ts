/** 开源仓库里的文档地址: 界面上的「开发指南 / 语法标准」跳到这里 (文档不打进镜像, 以仓库 main 为准)。 */
export const REPO_URL = 'https://github.com/wuyangfan234/zentrade'
const doc = (name: string) => `${REPO_URL}/blob/main/docs/${name}`

export const DOC_LINKS = {
  strategyGuide: doc('strategy-development.md'),
  strategyStandard: doc('strategy-standard.md'),
  strategyOverview: doc('strategy.md'),
} as const
