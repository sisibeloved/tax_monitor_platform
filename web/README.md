# 第一阶段 Web 驾驶舱

Web 应用基于 React、TypeScript、Vite、Ant Design 和 TanStack Query 构建，用于展示已持久化的
季度所得税风险监测结果。

## 环境准备

安装 Node.js 22，并在仓库根目录严格按照锁定文件安装依赖：

```bash
cd web
npm ci
```

验收构建或发布构建不得使用 `npm install`，因为该命令可能改写锁定文件。

## 环境变量

`VITE_API_BASE_URL` 是构建时使用的公共 URL 前缀。默认值为空，因此浏览器使用同源地址发起请求，
Compose Nginx 将 `/api/` 代理至 API：

```bash
export VITE_API_BASE_URL=''
```

本地集成访问地址为 `http://127.0.0.1:8080/`。Nginx 仅在运行时注入固定的本地验收 `Principal`；
严禁将 `DEVELOPMENT_PRINCIPAL_SECRET` 写入任何 `VITE_*` 变量、JavaScript 源代码、浏览器存储或
构建产物。生产环境必须替换所有本地密钥并使用经批准的 IdP 验证器；缺少验证器时，API 必须拒绝访问。

## 本地开发

进行组件开发时：

```bash
cd web
npm run dev -- --host 127.0.0.1
```

Vite 开发服务器不提供本地 API 代理，也不注入身份。组件测试应使用模拟响应；集成开发应使用 Compose
同源技术栈：

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d --build
```

## 单元测试

单次运行 Vitest 和 Testing Library，或在开发过程中以监听模式运行：

```bash
cd web
npm test -- --run
npm test
```

单元测试和组件测试可以在浏览器边界模拟 HTTP，但仍必须断言精确的金额字符串、URL 年度/季度筛选条件、
阻断与风险状态的区分，以及公式证据的渲染结果。

## 代码检查与类型检查

运行 ESLint 和 TypeScript 编译器，但不生成文件：

```bash
cd web
npm run lint
npx tsc --noEmit
npx tsc -p tsconfig.e2e.json --noEmit
```

## 构建

生成生产构建产物。脚本会先执行类型检查，再调用 Vite：

```bash
cd web
npm run build
```

容器构建使用 `npm ci`，仅将 `dist` 复制到非特权 Nginx 中，并通过端口 8080 提供服务。
Compose 同源部署必须保持 `VITE_API_BASE_URL` 为空。

## 浏览器端到端测试

Playwright 不会启动服务或注入数据。应先启动 Compose，再按照 `infra/README.md` 执行后端外部 E2E
数据注入。该流程必须通过真实的 API、PostgreSQL、Redis 和 `worker-quarterly` 完成，并输出唯一的
标准公司代码。

执行不使用 HTTP 模拟的真实浏览器测试：

```bash
cd web
export PLAYWRIGHT_BASE_URL=http://127.0.0.1:8080
export E2E_STANDARD_COMPANY_CODE='<外部后端E2E输出的公司代码>'
npm run test:e2e
```

测试文件为 `web/e2e/quarterly-dashboard.spec.ts`。本地未运行外部数据注入时，该项真实技术栈测试会明确
标记为跳过；这与当前没有真实数据接口的离线开发边界一致，不能作为生产验收证据。试点和生产候选验证时
必须设置标准公司代码并要求该测试实际通过。测试会定位已注入的 2026 年第2季度公司，打开公式详情，并通过
运行中的技术栈验证已持久化的源数据值和版本。

## 季度监测看板

驾驶舱访问地址如下：

```text
http://127.0.0.1:8080/?fiscal_year=2026&quarter=2
```

年度和季度筛选条件保存在 URL 查询参数中。页面展示监测覆盖情况、数据就绪情况、阻断公司、风险公司、
潜在风险估算、风险事项和公式抽屉。金额和税率以精确的 API 字符串传入；浏览器仅负责格式化，绝不重新计算
税务公式。公式详情来自 `GET /api/v1/detections/{id}`，并保留快照、数据源、tax-master 和规则版本血缘。
