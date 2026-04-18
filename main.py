#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon Beauty Bestseller Tracker
Telegram webhook and scheduled crawler for Amazon beauty bestsellers.
"""

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class AmazonBestsellerTracker:
    def __init__(self, config_file="config.json"):
        self.config = self._load_config(config_file)
        self.data_file = Path(self.config["data_file"])
        self.previous_data_file = Path(self.config["previous_data_file"])
        self.state_file = Path(self.config["state_file"])
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler = BackgroundScheduler()
        self.state = self.load_state()

    def _load_config(self, config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def load_state(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                logging.warning("State file is invalid. Resetting state.")
        return {'brands': [], 'chat_ids': []}

    def save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def fetch_bestsellers(self):
        try:
            headers = {'User-Agent': self.config['user_agent']}
            url = self.config['url']
            all_items = []
            seen_keys = set()
            page_count = 0

            while url and len(all_items) < 100 and page_count < 5:
                response = requests.get(url, headers=headers, timeout=15)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'lxml')
                page_items = self._parse_bestseller_items(soup)

                for item in page_items:
                    key = item.get('asin') or item['title'].strip().lower()
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    all_items.append(item)
                    if len(all_items) >= 100:
                        break

                next_link = soup.select_one('li.a-last a, a#pagnNextLink, a.a-last')
                if next_link and next_link.get('href'):
                    url = requests.compat.urljoin(url, next_link['href'])
                    page_count += 1
                else:
                    break

            return all_items[:100]
        except requests.RequestException as e:
            logging.error(f"Error fetching bestsellers: {e}")
            return None

    def _parse_bestseller_items(self, soup):
        containers = soup.select(
            'ol#zg-ordered-list li, div.zg-grid-general-faceout, li.zg-item-immersion, div.p13n-sc-uncoverable-faceout, div.s-result-item, div.zg_itemWrapper'
        )

        bestsellers = []
        for idx, item in enumerate(containers, 1):
            parsed = self._extract_item_info(item, idx)
            if not parsed:
                continue
            bestsellers.append(parsed)
            if len(bestsellers) >= 100:
                break

        return bestsellers

    def _extract_item_info(self, item, default_rank):
        asin = item.get('data-asin') or item.select_one('[data-asin]') and item.select_one('[data-asin]').get('data-asin')

        rank = None
        rank_elem = item.select_one('span.zg-badge-text, span.a-badge-text, span.zg-badge-text, span.a-list-item, span._cDEzb_p13n-sc-price')
        if rank_elem:
            rank_text = rank_elem.get_text(strip=True).replace('#', '').strip()
            if rank_text.isdigit():
                rank = int(rank_text)
        if rank is None:
            rank = default_rank

        title = None
        title_elem = item.select_one(
            'img[alt], div.p13n-sc-truncate, div.p13n-sc-truncate-desktop-type2, span.a-size-medium, h2, a.a-link-normal > span, span.a-size-base-plus'
        )
        if title_elem:
            if title_elem.name == 'img':
                title = title_elem.get('alt', '').strip()
            else:
                title = title_elem.get_text(strip=True)
        if not title:
            alt = item.find('img')
            title = alt.get('alt', '').strip() if alt else None
        if not title:
            return None

        price_elem = item.select_one('span.p13n-sc-price, span.a-price-whole, span.a-offscreen, span.a-color-price')
        price = price_elem.get_text(strip=True) if price_elem else 'N/A'

        rating_elem = item.select_one('span.a-icon-alt, i.a-icon-star-small span')
        rating = rating_elem.get_text(strip=True) if rating_elem else 'N/A'

        reviews_elem = item.select_one('a.a-size-small.a-link-normal') or item.select_one('span.a-size-small')
        reviews = reviews_elem.get_text(strip=True) if reviews_elem else 'N/A'

        return {
            'rank': rank,
            'title': title,
            'price': price,
            'rating': rating,
            'reviews': reviews,
            'asin': asin,
        }

    def save_data(self, bestsellers):
        try:
            if self.data_file.exists():
                try:
                    self.previous_data_file.write_text(self.data_file.read_text(encoding='utf-8'), encoding='utf-8')
                except Exception:
                    logging.warning('Unable to copy previous data file.')

            data = {'timestamp': datetime.now().isoformat(), 'bestsellers': bestsellers}
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logging.info(f"Data saved to {self.data_file}")
            return True
        except Exception as e:
            logging.error(f"Error saving data: {e}")
            return False

    def load_data(self, path=None):
        path = self.data_file if path is None else Path(path)
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading data from {path}: {e}")
            return None

    def compare_brand_ranks(self, old_data, new_data):
        brands = self.state.get('brands', [])
        results = []
        for brand in brands:
            brand_lower = brand.lower()
            old_matches = []
            new_matches = []
            if old_data and 'bestsellers' in old_data:
                old_matches = [item for item in old_data['bestsellers'] if brand_lower in item['title'].lower()]
            if new_data and 'bestsellers' in new_data:
                new_matches = [item for item in new_data['bestsellers'] if brand_lower in item['title'].lower()]

            old_rank = min((item['rank'] for item in old_matches), default=None)
            new_rank = min((item['rank'] for item in new_matches), default=None)

            if old_rank is None and new_rank is None:
                status = 'missing'
            elif old_rank is None:
                status = 'new'
            elif new_rank is None:
                status = 'dropped'
            elif old_rank == new_rank:
                status = 'same'
            elif new_rank < old_rank:
                status = 'up'
            else:
                status = 'down'

            results.append({
                'brand': brand,
                'status': status,
                'old_rank': old_rank,
                'new_rank': new_rank,
                'titles': [item['title'] for item in new_matches[:2]] if new_matches else [],
            })

        return results

    def build_update_message(self, old_data, new_data):
        if not self.state.get('brands'):
            return '📌 현재 추적 중인 브랜드가 없습니다. /add <브랜드> 로 브랜드를 추가해주세요.'

        changes = self.compare_brand_ranks(old_data, new_data)
        lines = [f"📣 Amazon Beauty Bestseller 업데이트", f"시간: {new_data.get('timestamp', 'N/A')}"]

        for entry in changes:
            brand = entry['brand']
            if entry['status'] == 'new':
                lines.append(f"- {brand}: 새로 발견됨, 현재 순위 {entry['new_rank']}")
            elif entry['status'] == 'dropped':
                lines.append(f"- {brand}: 이전에는 순위 {entry['old_rank']}였으나 현재 목록에서는 제외됨")
            elif entry['status'] == 'same':
                lines.append(f"- {brand}: 순위 변동 없음 ({entry['new_rank']})")
            elif entry['status'] == 'up':
                diff = entry['old_rank'] - entry['new_rank']
                lines.append(f"- {brand}: 순위 상승 {diff}위 ({entry['old_rank']} → {entry['new_rank']})")
            elif entry['status'] == 'down':
                diff = entry['new_rank'] - entry['old_rank']
                lines.append(f"- {brand}: 순위 하락 {diff}위 ({entry['old_rank']} → {entry['new_rank']})")
            else:
                lines.append(f"- {brand}: 현재 목록에서 찾을 수 없습니다.")

        return '\n'.join(lines)

    def build_summary_text(self):
        data = self.load_data()
        if not data or 'bestsellers' not in data:
            return '🔍 아직 수집된 베스트셀러 데이터가 없습니다. 먼저 업데이트를 실행해주세요.'

        lines = [f"📋 현재 추적 브랜드 요약", f"수집 시간: {data.get('timestamp', 'N/A')}\n"]
        brands = self.state.get('brands', [])
        if not brands:
            lines.append('추적 중인 브랜드가 없습니다. /add <브랜드> 로 브랜드를 추가하세요.')
            return '\n'.join(lines)

        for brand in brands:
            brand_lower = brand.lower()
            matches = [item for item in data['bestsellers'] if brand_lower in item['title'].lower()]
            if matches:
                rank = min(item['rank'] for item in matches)
                lines.append(f"- {brand}: 현재 순위 {rank} (상품 예: {matches[0]['title'][:50]})")
            else:
                lines.append(f"- {brand}: 현재 순위 목록에 없음")

        lines.append('\n추적 중인 브랜드: ' + ', '.join(brands))
        return '\n'.join(lines)

    def get_telegram_token(self):
        return os.getenv('TELEGRAM_BOT_TOKEN')

    def send_telegram_message(self, chat_id, text):
        token = self.get_telegram_token()
        if not token:
            logging.warning('Telegram bot token is not configured. Skipping Telegram notification.')
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {'chat_id': chat_id, 'text': text}
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code != 200:
                logging.error(f"Telegram error: {response.status_code} {response.text}")
                return False
            return True
        except requests.RequestException as e:
            logging.error(f"Error sending Telegram message: {e}")
            return False

    def broadcast_message(self, text):
        if not self.get_telegram_token():
            logging.warning('Telegram bot token missing. Broadcast skipped.')
            return

        for chat_id in self.state.get('chat_ids', []):
            self.send_telegram_message(chat_id, text)

    def add_brand(self, brand):
        normalized = brand.strip()
        if not normalized:
            return False
        exists = any(normalized.lower() == existing.lower() for existing in self.state.get('brands', []))
        if not exists:
            self.state['brands'].append(normalized)
            self.save_state()
        return not exists

    def remove_brand(self, brand):
        normalized = brand.strip().lower()
        before = len(self.state.get('brands', []))
        self.state['brands'] = [existing for existing in self.state['brands'] if existing.lower() != normalized]
        if len(self.state['brands']) != before:
            self.save_state()
            return True
        return False

    def add_chat_id(self, chat_id):
        if chat_id not in self.state.get('chat_ids', []):
            self.state['chat_ids'].append(chat_id)
            self.save_state()
            return True
        return False

    def handle_telegram_command(self, chat_id, text):
        command = text.strip()
        if not command:
            return self.send_telegram_message(chat_id, self.make_help_text())

        parts = command.split(maxsplit=1)
        action = parts[0].lower().lstrip('/')
        argument = parts[1].strip() if len(parts) > 1 else ''

        if action in ['start', '시작']:
            self.add_chat_id(chat_id)
            self.ensure_data_available()
            return self.send_telegram_message(chat_id, '✅ 알림 등록이 완료되었습니다. 6시간마다 추적 브랜드 순위 변동을 전송합니다.')

        if action in ['add', '추가']:
            if not argument:
                return self.send_telegram_message(chat_id, '사용법: /add <브랜드> 또는 추가 <브랜드>')
            self.ensure_data_available()
            added = self.add_brand(argument)
            if added:
                return self.send_telegram_message(chat_id, f"✅ 브랜드 '{argument}' 이(가) 추적 목록에 추가되었습니다.")
            return self.send_telegram_message(chat_id, f"⚠️ 브랜드 '{argument}' 은(는) 이미 추적 중입니다.")

        if action in ['remove', '삭제', 'del', 'delete']:
            if not argument:
                return self.send_telegram_message(chat_id, '사용법: /remove <브랜드> 또는 삭제 <브랜드>')
            removed = self.remove_brand(argument)
            if removed:
                return self.send_telegram_message(chat_id, f"✅ 브랜드 '{argument}' 이(가) 추적 목록에서 제거되었습니다.")
            return self.send_telegram_message(chat_id, f"⚠️ 브랜드 '{argument}' 을(를) 찾을 수 없습니다.")

        if action in ['update', '업데이트']:
            self.update()
            return self.send_telegram_message(chat_id, '🔄 즉시 업데이트를 시작했습니다. 완료되면 추적 중인 브랜드 순위 변동을 전송합니다.')

        if action in ['summary', '요약']:
            self.ensure_data_available()
            return self.send_telegram_message(chat_id, self.build_summary_text())

        if action in ['help', '도움', '도움말']:
            return self.send_telegram_message(chat_id, self.make_help_text())

        if action in ['list', '목록']:
            brands = self.state.get('brands', [])
            if brands:
                return self.send_telegram_message(chat_id, '추적 중인 브랜드:\n' + '\n'.join(f'- {brand}' for brand in brands))
            return self.send_telegram_message(chat_id, '추적 중인 브랜드가 없습니다. /add <브랜드> 로 추가해주세요.')

        return self.send_telegram_message(chat_id, self.make_help_text())

    def make_help_text(self):
        return (
            '사용 가능한 명령어:\n'
            '/start 또는 시작 - 알림 등록\n'
            '/add <브랜드> 또는 추가 <브랜드> - 브랜드 추가\n'
            '/remove <브랜드> 또는 삭제 <브랜드> - 브랜드 제거\n'
            '/update 또는 업데이트 - 즉시 크롤링 및 업데이트 실행\n'
            '/summary 또는 요약 - 현재 요약 정보 수신\n'
            '/list 또는 목록 - 추적 중인 브랜드 보기\n'
            '/help 또는 도움 - 도움말 보기'
        )

    def ensure_data_available(self):
        data = self.load_data()
        if data and data.get('bestsellers'):
            return data

        logging.info('No bestseller data available. Running update now...')
        self.update()
        return self.load_data()

    def update(self):
        logging.info('Updating bestseller data...')
        previous_data = self.load_data(self.data_file)
        bestsellers = self.fetch_bestsellers()
        if bestsellers:
            saved = self.save_data(bestsellers)
            if saved:
                logging.info(f'Successfully updated with {len(bestsellers)} products')
                if self.state.get('chat_ids'):
                    message = self.build_update_message(previous_data, {'timestamp': datetime.now().isoformat(), 'bestsellers': bestsellers})
                    self.broadcast_message(message)
        else:
            logging.warning('Failed to update data')

    def start_auto_update(self):
        interval_hours = self.config.get('update_interval_hours', 6)
        self.scheduler.add_job(
            self.update,
            'interval',
            hours=interval_hours,
            next_run_time=datetime.now(),
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        self.scheduler.start()
        logging.info(f'Auto-update started (every {interval_hours} hours)')

    def stop_auto_update(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logging.info('Auto-update stopped')


def parse_args():
    parser = argparse.ArgumentParser(description='Amazon Bestseller Tracker CI runner')
    parser.add_argument('--serve', action='store_true', help='Run webhook server')
    parser.add_argument('--telegram-chat-id', dest='telegram_chat_id', help='Telegram chat id from CI event')
    parser.add_argument('--telegram-text', dest='telegram_text', help='Telegram text from CI event')
    parser.add_argument('--update-now', action='store_true', help='Run an immediate update')
    return parser.parse_args()


def run_ci_mode(tracker, args):
    chat_id = args.telegram_chat_id or os.getenv('TELEGRAM_CHAT_ID')
    text = args.telegram_text or os.getenv('TELEGRAM_TEXT')

    logging.info(f'CI payload chat_id={chat_id!r}, text={text!r}, update_now={args.update_now}, serve={args.serve}')

    if args.update_now:
        tracker.update()
        return

    if text:
        if not chat_id:
            logging.error('CI mode requires TELEGRAM_CHAT_ID or --telegram-chat-id when TELEGRAM_TEXT is provided.')
            return
        tracker.handle_telegram_command(chat_id, text)
        return

    if args.serve:
        tracker.start_auto_update()
        app.run(host='0.0.0.0', port=5000, threaded=True)
        return

    logging.info('No CI mode arguments provided. Use --help for usage information.')


tracker = AmazonBestsellerTracker()


@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json(silent=True)
    if not update:
        return jsonify({'ok': False, 'error': 'invalid payload'}), 400

    message = update.get('message') or update.get('edited_message')
    if not message:
        return jsonify({'ok': True, 'status': 'no message'}), 200

    chat = message.get('chat', {})
    chat_id = chat.get('id')
    text = message.get('text', '').strip()

    if not chat_id or not text:
        return jsonify({'ok': True, 'status': 'ignored'}), 200

    tracker.handle_telegram_command(chat_id, text)
    return jsonify({'ok': True}), 200


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    args = parse_args()
    if args.serve:
        logging.info('Starting Amazon Bestseller Tracker service...')
    run_ci_mode(tracker, args)
