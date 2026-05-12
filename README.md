# Product System — Phase 1-A

楽天 × Shopify 在庫・需要予測・自動発注システム

このリポジトリは **Phase 1-A：取り込み + マスター在庫管理** の実装です。
楽天市場および Shopify からの注文を中央DB（PostgreSQL）に集約し、
イベントソーシング型で在庫を管理します。

詳細仕様は [`docs/01_requirements_definition.md`](docs/01_requirements_definition.md) を参照してください。

## アーキテクチャ概要

- **レイヤード構成**: `api` → `services` → `adapters` / `models` / `db`
- **チャネル抽象化**: `ChannelAdapter` ABC で楽天 / Shopify / (将来 Amazon・卸) を共通I/F化
- **非同期キュー抽象化**: `TaskQueue` プロトコルで Cloud Tasks とローカル即時実行を切替
- **イベントソーシング**: 在庫変動はすべて `inventory_events` に追記、スナップショットは導出

## 必要環境

- Python 3.11+
- Docker / Docker Compose（ローカル PostgreSQL 用）

## クイックスタート

```powershell
# 1. 仮想環境
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

# 2. 依存インストール
pip install -e ".[dev]"

# 3. 環境変数
Copy-Item .env.example .env

# 4. ローカル DB 起動
docker compose up -d db

# 5. マイグレーション
alembic upgrade head

# 6. アプリ起動
uvicorn app.main:app --reload

# 7. テスト
pytest
```

## ディレクトリ構成

```
app/
├── api/          # FastAPI ルーター（HTTP境界）
├── services/     # ユースケース / ビジネスロジック
├── adapters/     # チャネル接続（楽天 / Shopify）
├── models/       # SQLAlchemy ORM
├── db/           # セッション / リポジトリ
├── queue/        # 非同期タスク抽象（Cloud Tasks / In-memory）
├── ui/           # 管理画面（Jinja2 + Tailwind）
├── config.py     # 設定（pydantic-settings）
└── main.py       # FastAPI エントリポイント

alembic/          # DB マイグレーション
tests/            # pytest
docs/             # 要件定義 / 設計ドキュメント
```

## 開発コマンド

| コマンド | 用途 |
|---|---|
| `ruff check .` | Lint |
| `ruff format .` | フォーマット |
| `mypy app` | 型チェック |
| `pytest` | テスト |
| `alembic revision --autogenerate -m "msg"` | マイグレーション生成 |
| `alembic upgrade head` | マイグレーション適用 |

## フェーズ進行

| Sprint | 内容 | 状態 |
|---|---|---|
| 0 | プロジェクト基盤・ローカル開発環境 | **進行中** |
| 1 | データモデル & 在庫減算コアロジック | 未着手 |
| 2 | Adapter基盤 & 取込パイプライン | 未着手 |
| 3 | 管理画面（4画面） | 未着手 |
| 4 | BigQuery export & Terraform 骨格 | 未着手 |
| 5 | クライアント情報受領後の本番疎通 | クライアント情報待ち |
