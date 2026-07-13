# README 与 CHANGELOG 阶段版本化实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以中文仓库首页和阶段化版本历史清晰呈现集团所得税风险监测平台的能力、Agent 工作流、验收边界及第一至第四阶段交付内容。

**Architecture:** 仅修改仓库级文档，不改变运行代码。根 README 作为读者入口，保留启动、验证和运维命令并新增能力/流程/样例总览；根 CHANGELOG 按语义化版本映射四个开发阶段，详细设计和手册继续作为单一事实来源。

**Tech Stack:** Markdown、Keep a Changelog 1.1.0、Semantic Versioning、Git。

---

## Chunk 1: 仓库文档与交付

### Task 1: 重构仓库首页

**Files:**
- Modify: `README.md`
- Reference: `docs/superpowers/specs/2026-07-13-readme-changelog-versioning-design.md`
- Reference: `docs/operations/acceptance-scorecard.md`
- Reference: `artifacts/acceptance/phase-4/uat-scorecard.json`

- [x] **Step 1: 增加项目元信息与能力入口**

在标题下增加 `0.4.0`、`technical_ready`、`LOCAL_SYNTHETIC`、Python、React 徽章；随后用表格概括季度计提、累计税负、潜在税务成本、月度科目准确性四类监测的频率、判断和输出。

- [x] **Step 2: 增加 Agent 工作流和验收场景**

用一条季度流程和一条月度流程说明受控数据、确定性公式、专业 Agent、风险清单、人工复核及审计闭环；列出标准季度数值样例、业务招待费正反例、福利费与捐赠典型改账建议、105/126家公司批量场景，并明确本地合成证据边界。

- [x] **Step 3: 保留并整理操作入口**

按参考仓库的信息层次整理目录树、本地启动、验证、监控、发布升级、备份回滚、生产准入和版本历史；链接现有中文设计、操作手册、验收评分卡及新增 CHANGELOG。

- [x] **Step 4: 检查 README 差异**

Run: `git diff --check -- README.md`

Expected: exit 0，无空白错误。

### Task 2: 新增阶段化版本历史

**Files:**
- Create: `CHANGELOG.md`
- Reference: `README.md`
- Reference: `git log --reverse --date=short --format='%h %ad %s' --all`

- [x] **Step 1: 建立版本历史格式**

使用中文导言、Keep a Changelog 1.1.0 和 Semantic Versioning，保留 `Unreleased`，以用户可感知能力为单位记录变更，不逐条抄写提交日志。

- [x] **Step 2: 记录四个阶段**

按 `0.1.0` 季度监测、`0.2.0` 业务招待费、`0.3.0` 福利费/捐赠、`0.4.0` 治理与运维记录新增、变更和修复；日期均取仓库提交日期 `2026-07-13`。

- [x] **Step 3: 检查 CHANGELOG 差异**

Run: `git diff --check -- CHANGELOG.md`

Expected: exit 0，无空白错误。

### Task 3: 验证并提交文档

**Files:**
- Modify: `README.md`
- Create: `CHANGELOG.md`
- Create: `docs/superpowers/plans/2026-07-13-readme-changelog-versioning.md`

- [x] **Step 1: 验证本地 Markdown 链接**

Run: `python3 - <<'PY'`，解析 `README.md` 和 `CHANGELOG.md` 中非 HTTP、非锚点链接，断言目标路径存在。

Expected: 输出 `Markdown links verified`，exit 0。

- [x] **Step 2: 运行文档相关门禁**

Run: `make verify-governance`

Expected: exit 0。

Run: `make verify-release`

Expected: exit 0，清单签名模式仍为非生产临时签名。

- [x] **Step 3: 提交文档**

```bash
git add README.md CHANGELOG.md docs/superpowers/plans/2026-07-13-readme-changelog-versioning.md
git commit -m "docs: publish phased platform guide and changelog"
```

### Task 4: 完整复验、合并并推送

**Files:**
- Verify only: repository-wide source, tests and generated acceptance artifacts

- [ ] **Step 1: 获取远端并确认可安全合并**

Run: `git fetch origin`

Expected: exit 0；确认 `origin/main` 没有无法解释的新提交，本地主分支和功能分支工作树干净。

- [ ] **Step 2: 在功能分支运行完整验证**

Run: `make test-backend && make test-web && make verify-governance && make security-check && make verify-migrations && make verify-release && make verify-capacity COMPANY_FIXTURE=126 && make verify-rollback && make uat SNAPSHOT_SET=pilot-2026q2`

Expected: 所有命令 exit 0；允许仅跳过依赖真实外部 API/消息代理/工作进程的季度 E2E，UAT 保持 `technical_ready=true`、`production_ready=false`。

- [ ] **Step 3: 合并至主分支**

在根工作树确认 `main` 干净，快进合并 `codex/phase-4-governance-hardening`，不创建额外合并提交。

Run: `git merge --ff-only codex/phase-4-governance-hardening`

Expected: exit 0，`main` 指向已验证的功能分支提交。

- [ ] **Step 4: 在主分支复验**

Run: `make verify-governance && make security-check && make verify-release && make uat SNAPSHOT_SET=pilot-2026q2`

Expected: 所有命令 exit 0，工作树干净，UAT 准入结论未漂移。

- [ ] **Step 5: 推送主分支**

Run: `git push origin main`

Expected: exit 0，`origin/main` 更新到本地 `main`，本地与远端提交一致。
