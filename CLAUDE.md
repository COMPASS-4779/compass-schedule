# CLAUDE.md — compass-schedule 開発ワークフロー

## ブランチ戦略

| ブランチ | 役割 | Renderサービス |
|---|---|---|
| `develop` | 開発・テスト用 | compass-schedule-dev（テスト環境） |
| `main` | 本番用 | compass-schedule（本番環境） |

## 開発フロー

```
1. develop ブランチで修正・開発
2. Renderテスト環境（compass-schedule-dev）で動作確認
3. 問題なければ PR を作成: develop → main
4. PR マージ → 本番（compass-schedule）に自動デプロイ
```

## 環境変数

各Renderサービスで以下を個別に設定する：

| 変数名 | テスト環境 | 本番環境 |
|---|---|---|
| `DATABASE_URL` | Render PostgreSQL（テスト用DB） | Render PostgreSQL（本番DB） |
| `ENVIRONMENT` | `development` | `production` |

## 主要ファイル

| ファイル | 内容 |
|---|---|
| `index.html` | フロントエンド（HTML/CSS/JS 単一ファイル） |
| `main.py` | FastAPI バックエンド |
| `database.py` | DB接続（DATABASE_URL 環境変数で切替） |
| `models.py` | データモデル |
| `schemas.py` | スキーマ定義 |
| `Procfile.txt` | Renderデプロイ設定 |

## 作業の原則

- `develop` ブランチに直接プッシュして良い（個人開発のため）
- `main` へは必ずテスト確認後にマージする
- 変更前に `index.html.bak` バックアップを作成する
- `main.py` / `database.py` / `models.py` / `schemas.py` は慎重に変更する
