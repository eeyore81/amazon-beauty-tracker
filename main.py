#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon Beauty Bestseller Tracker
Telegram webhook and scheduled crawler for Amazon beauty bestsellers.
"""

import argparse
import io
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
import threading
import html
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class AmazonBestsellerTracker:
    def __init__(self, config_file="config.json"):
        self.config = self._load_config(config_file)
        self.bestseller_url = self.config["url"]
        self.movers_url = self.config.get(
            "movers_url",
            "https://www.amazon.com/-/ko/gp/movers-and-shakers/beauty/ref=zg_bs_tab_t_beauty_bsms",
        )
        self.data_file = Path(self.config["data_file"])
        self.previous_data_file = Path(self.config["previous_data_file"])
        self.movers_data_file = Path(self.config.get("movers_data_file", "data/movers.json"))
        self.previous_movers_data_file = Path(self.config.get("previous_movers_data_file", "data/movers_prev.json"))
        self.state_file = Path(self.config["state_file"])
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.movers_data_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler = BackgroundScheduler()
        self.update_lock = threading.Lock()
        self.asin_detail_cache = {}
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
        # Force Playwright-only collection as requested.
        self.config['use_browser'] = True
        browser_items = self.fetch_bestsellers_with_browser(self.bestseller_url, page_2_url=self.config.get('page_2_url'))
        if browser_items:
            return browser_items
        logging.error('Playwright scraping failed and no non-browser fallback is allowed.')
        return None

    def fetch_movers_and_shakers(self):
        # Keep the same Playwright-only strategy used by bestseller crawling.
        self.config['use_browser'] = True
        browser_items = self.fetch_bestsellers_with_browser(self.movers_url, page_2_url=self.config.get('movers_page_2_url'))
        if browser_items:
            return browser_items
        logging.error('Playwright scraping failed for movers and shakers data.')
        return None

    def _build_page_url(self, base_url, page_number):
        if page_number <= 1:
            return base_url
        separator = '&' if '?' in base_url else '?'
        return f'{base_url}{separator}pg={page_number}'

    def _count_placeholder_items(self, items):
        if not items:
            return 0
        return sum(1 for item in items if str(item.get('title', '')).startswith('ASIN '))

    def _enrich_page_items_from_client_recs(self, soup, page_items, session):
        rank_pairs = self._extract_client_recs_ranks(soup)
        if not rank_pairs:
            return page_items

        by_asin = {}
        for item in page_items:
            asin = item.get('asin')
            if asin:
                by_asin[asin] = item

        enriched = []
        for asin, rank in rank_pairs:
            existing = by_asin.get(asin)
            if existing:
                existing['rank'] = rank
                enriched.append(existing)
                continue

            details = self._fetch_item_details_from_asin(asin, session)
            if details:
                details['rank'] = rank
                details['asin'] = asin
                enriched.append(details)
            else:
                enriched.append(
                    {
                        'rank': rank,
                        'title': f'ASIN {asin}',
                        'price': 'N/A',
                        'rating': 'N/A',
                        'reviews': 'N/A',
                        'asin': asin,
                        'image': None,
                    }
                )

        seen = set()
        deduped = []
        for item in sorted(enriched, key=lambda x: x['rank']):
            asin = item.get('asin')
            if asin in seen:
                continue
            seen.add(asin)
            deduped.append(item)
        return deduped

    def _extract_client_recs_ranks(self, soup):
        raw_candidates = []

        rec_nodes = soup.select('[data-client-recs-list]')
        for node in rec_nodes:
            raw_value = node.get('data-client-recs-list') or ''
            if raw_value:
                raw_candidates.append(raw_value)

        if not raw_candidates:
            html_text = str(soup)
            raw_candidates.extend(re.findall(r'data-client-recs-list="([^"]+)"', html_text))
            raw_candidates.extend(re.findall(r"data-client-recs-list='([^']+)'", html_text))

        if not raw_candidates:
            return []

        best_ranks = []
        for raw in sorted(raw_candidates, key=len, reverse=True):
            ranks = self._parse_client_recs_blob(raw)
            if len(ranks) > len(best_ranks):
                best_ranks = ranks
            if len(best_ranks) >= 50:
                break

        return best_ranks

    def _parse_client_recs_blob(self, raw):
        if not raw:
            return []

        decoded = html.unescape(raw)
        candidates = [decoded, decoded.replace('&quot;', '"')]

        if '[' in decoded and ']' in decoded:
            start = decoded.find('[')
            end = decoded.rfind(']')
            if start >= 0 and end > start:
                candidates.append(decoded[start:end + 1])

        data = None
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    data = parsed
                    break
            except Exception:
                continue

        if not data:
            return []

        ranks = []
        for entry in data:
            asin = entry.get('id')
            meta = entry.get('metadataMap') or {}
            rank_text = meta.get('render.zg.rank')
            if not asin or not rank_text:
                continue
            try:
                rank = int(str(rank_text).strip())
            except ValueError:
                continue
            ranks.append((asin, rank))

        ranks.sort(key=lambda x: x[1])
        return ranks

    def _fetch_item_details_from_asin(self, asin, session):
        cached = self.asin_detail_cache.get(asin)
        if cached:
            return dict(cached)

        url = f'https://www.amazon.com/dp/{asin}'
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'lxml')

            title_elem = soup.select_one('#productTitle') or soup.select_one('meta[property="og:title"]')
            if not title_elem:
                return None
            if title_elem.name == 'meta':
                title = (title_elem.get('content') or '').strip()
            else:
                title = title_elem.get_text(' ', strip=True)
            if not title:
                return None

            image_elem = soup.select_one('#landingImage') or soup.select_one('meta[property="og:image"]')
            if image_elem and image_elem.name == 'meta':
                image = image_elem.get('content')
            else:
                image = image_elem.get('src') if image_elem else None

            price_elem = (
                soup.select_one('span.a-price span.a-offscreen')
                or soup.select_one('#corePrice_feature_div span.a-offscreen')
                or soup.select_one('meta[property="product:price:amount"]')
            )
            if price_elem and price_elem.name == 'meta':
                price = price_elem.get('content', 'N/A')
            else:
                price = price_elem.get_text(strip=True) if price_elem else 'N/A'

            rating_elem = soup.select_one('#acrPopover') or soup.select_one('span.a-icon-alt')
            rating = (
                rating_elem.get('title', '').strip()
                or rating_elem.get_text(strip=True)
                if rating_elem
                else 'N/A'
            )
            rating = rating or 'N/A'

            reviews_elem = soup.select_one('#acrCustomerReviewText')
            reviews = reviews_elem.get_text(strip=True) if reviews_elem else 'N/A'

            details = {
                'title': title,
                'price': price,
                'rating': rating,
                'reviews': reviews,
                'image': image,
            }
            self.asin_detail_cache[asin] = details
            return dict(details)
        except Exception:
            search_details = self._fetch_item_details_from_search(asin, session)
            if search_details:
                self.asin_detail_cache[asin] = search_details
                return dict(search_details)
            return None

    def _fetch_item_details_from_search(self, asin, session):
        try:
            response = session.get(f'https://www.amazon.com/s?k={asin}', timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'lxml')

            # Prefer exact ASIN card if present.
            card = soup.select_one(f'div.s-result-item[data-asin="{asin}"]')
            if not card:
                cards = [node for node in soup.select('div.s-result-item[data-asin]') if node.get('data-asin')]
                card = cards[0] if cards else None
            if not card:
                return None

            title_elem = card.select_one('h2 a span') or card.select_one('span.a-size-medium')
            title = title_elem.get_text(' ', strip=True) if title_elem else ''
            if not title:
                return None

            image_elem = card.select_one('img.s-image') or card.select_one('img')
            image = image_elem.get('src') if image_elem else None

            price_elem = card.select_one('span.a-price span.a-offscreen') or card.select_one('span.a-price-whole')
            price = price_elem.get_text(strip=True) if price_elem else 'N/A'

            rating_elem = card.select_one('span.a-icon-alt')
            rating = rating_elem.get_text(strip=True) if rating_elem else 'N/A'

            reviews_elem = card.select_one('span.a-size-base.s-underline-text') or card.select_one('span.a-size-base')
            reviews = reviews_elem.get_text(strip=True) if reviews_elem else 'N/A'

            return {
                'title': title,
                'price': price,
                'rating': rating,
                'reviews': reviews,
                'image': image,
            }
        except Exception:
            return None

    def fetch_bestsellers_with_browser(self, base_url=None, page_2_url=None):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            logging.error(f"Browser automation dependencies missing: {e}")
            return None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.config['user_agent'],
                    viewport={'width': 1920, 'height': 1080},
                )
                page = context.new_page()

                items = []
                seen_keys = set()
                first_page = base_url or self.bestseller_url
                second_page = page_2_url or self._build_page_url(first_page, 2)
                page_urls = [first_page, second_page]

                for page_index, page_url in enumerate(page_urls, start=1):
                    if len(items) >= 100:
                        break

                    logging.info(f'Browser loading page {page_index}: {page_url}')
                    page.goto(page_url, timeout=45000, wait_until='domcontentloaded')
                    page.wait_for_timeout(self.config.get('browser_initial_wait', 4) * 1000)

                    self._browser_scroll_page(page, expected_min_items=50)
                    html = page.content()
                    soup = BeautifulSoup(html, 'lxml')
                    page_items = self._parse_bestseller_items(soup)

                    if len(page_items) < 50:
                        # Retry a few times after extra scrolling when CI/network is slow.
                        for _ in range(3):
                            self._browser_scroll_page(page, expected_min_items=50)
                            soup = BeautifulSoup(page.content(), 'lxml')
                            page_items = self._parse_bestseller_items(soup)
                            if len(page_items) >= 50:
                                break

                    for item in page_items:
                        key = item.get('asin') or item['title'].strip().lower()
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        items.append(item)
                        if len(items) >= 100:
                            break
                    logging.info(f'Browser page {page_index}: parsed {len(page_items)} items, total {len(items)} items')

                context.close()
                browser.close()
                return items[:100]
        except Exception as e:
            logging.error(f"Error fetching items with browser automation: {e}")
            return None

    def _browser_scroll_page(self, page, expected_min_items=50):
        scroll_steps = self.config.get('browser_scroll_steps', 12)
        scroll_delay = self.config.get('browser_scroll_delay', 2)
        for _ in range(scroll_steps):
            page.evaluate('window.scrollBy(0, window.innerHeight);')
            page.wait_for_timeout(int(scroll_delay * 1000))
        page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass
        self._wait_for_browser_items(page, expected_min_items)

    def _wait_for_browser_items(self, page, expected_min_items):
        selector = (
            'div.a-cardui._cDEzb_grid-cell_1uMOS.expandableGrid.p13n-grid-content,'
            'div._cDEzb_grid-cell_1uMOS.expandableGrid.p13n-grid-content,'
            'ol#zg-ordered-list > li,'
            'li.zg-item-immersion,'
            'div.p13n-sc-uncoverable-faceout,'
            'div.s-result-item,'
            'div.zg_itemWrapper'
        )

        stable_count = 0
        previous = -1
        for _ in range(8):
            try:
                current = page.locator(selector).count()
            except Exception:
                break

            if current >= expected_min_items:
                return

            if current == previous:
                stable_count += 1
            else:
                stable_count = 0
            previous = current

            if stable_count >= 2:
                page.evaluate('window.scrollBy(0, window.innerHeight * 0.7);')
            page.wait_for_timeout(900)

    def _is_playwright_available(self):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            return True
        except Exception:
            return False

    def _find_next_page_link(self, soup):
        next_selectors = [
            'li.a-last a',
            'a#pagnNextLink',
            'a.a-last',
            'a[href*="?pg="]',
            'a[href*="ref_=zg_bs_pg"]',
        ]
        for selector in next_selectors:
            element = soup.select_one(selector)
            if element and element.get('href'):
                return element['href']
        return None

    def _parse_bestseller_items(self, soup):
        containers = soup.select('div.a-cardui._cDEzb_grid-cell_1uMOS.expandableGrid.p13n-grid-content')
        if not containers:
            containers = soup.select('div._cDEzb_grid-cell_1uMOS.expandableGrid.p13n-grid-content')
        if not containers:
            containers = soup.select('ol#zg-ordered-list > li')
        if not containers:
            containers = soup.select('li.zg-item-immersion, div.p13n-sc-uncoverable-faceout')
        if not containers:
            containers = soup.select('div.s-result-item, div.zg_itemWrapper')

        bestsellers = []
        seen_ranks = set()
        for idx, item in enumerate(containers, 1):
            parsed = self._extract_item_info(item, idx)
            if not parsed:
                continue
            if parsed['rank'] in seen_ranks:
                continue
            seen_ranks.add(parsed['rank'])
            bestsellers.append(parsed)
            if len(bestsellers) >= 100:
                break

        return bestsellers

    def _extract_item_info(self, item, default_rank):
        asin = item.get('data-asin') or (item.select_one('[data-asin]') and item.select_one('[data-asin]').get('data-asin'))

        rank = None
        rank_elem = item.select_one('span.zg-bdg-text, span.zg-badge-text, span.a-badge-text, span.zg-badge-text, span.a-list-item, span._cDEzb_p13n-sc-price')
        if rank_elem:
            rank_text = rank_elem.get_text(strip=True)
            rank_digits = re.search(r'#?(\d{1,3})', rank_text)
            if rank_digits:
                rank = int(rank_digits.group(1))
        if rank is None:
            rank = default_rank

        title = None
        title_elem = item.select_one(
            'div._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, div.p13n-sc-truncate, div.p13n-sc-truncate-desktop-type2, span.a-size-medium, h2, a.a-link-normal > span, span.a-size-base-plus, img[alt]'
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

        image = self._extract_image_url(item)
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
            'image': image,
        }

    def _extract_image_url(self, item):
        image_elem = item.select_one('img[alt]') or item.select_one('img')
        if not image_elem:
            return None
        return image_elem.get('src') or image_elem.get('data-src') or image_elem.get('data-old-hires')

    def save_data(self, items, path=None, previous_path=None, data_key='bestsellers'):
        target_file = self.data_file if path is None else Path(path)
        previous_file = self.previous_data_file if previous_path is None else Path(previous_path)
        try:
            if target_file.exists():
                try:
                    previous_file.write_text(target_file.read_text(encoding='utf-8'), encoding='utf-8')
                except Exception:
                    logging.warning('Unable to copy previous data file.')

            data = {'timestamp': datetime.now().isoformat(), data_key: items}
            with open(target_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logging.info(f"Data saved to {target_file}")
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

        lines = [
            '📣 Amazon Beauty Bestseller 업데이트',
            f"시간: {new_data.get('timestamp', 'N/A')}",
            '────────────────────────',
        ]

        for brand in self.state.get('brands', []):
            brand_lower = brand.lower()
            current_matches = [item for item in new_data.get('bestsellers', []) if brand_lower in item['title'].lower()]
            if not current_matches:
                lines.append(f"❌ {brand}: 현재 목록에서 찾을 수 없음")
                continue

            lines.append(f"📌 {brand} ({len(current_matches)}개)")
            for item in sorted(current_matches, key=lambda x: x['rank']):
                old_item = self._find_previous_item(old_data, item)
                diff_text = self._format_rank_diff(old_item, item['rank'])
                lines.append(f"{item['rank']}위 {item['title']} {diff_text}".strip())
                if item.get('image'):
                    lines.append(item['image'])
            lines.append('')

        lines.append('────────────────────────')
        lines.append(f"총 추적 브랜드: {len(self.state.get('brands', []))}개")
        return '\n'.join(lines)

    def _find_previous_item(self, old_data, current_item, data_key='bestsellers'):
        if not old_data or data_key not in old_data:
            return None
        current_title = current_item['title'].strip().lower()
        candidates = [item for item in old_data[data_key] if item['title'].strip().lower() == current_title]
        return min(candidates, key=lambda x: x['rank']) if candidates else None

    def _format_rank_diff(self, old_item, new_rank):
        if not old_item:
            return '(새로 진입)'
        old_rank = old_item['rank']
        if old_rank == new_rank:
            return '(변동 없음)'
        diff = old_rank - new_rank
        if diff > 0:
            return f'(상승: {diff})'
        return f'(하락: {abs(diff)})'

    def _format_rank_diff_en(self, old_item, new_rank):
        if not old_item:
            return '(New)'
        old_rank = old_item['rank']
        if old_rank == new_rank:
            return '(No change)'
        diff = old_rank - new_rank
        if diff > 0:
            return f'(Up {diff})'
        return f'(Down {abs(diff)})'

    def build_summary_text(self):
        data = self.load_data()
        if not data or 'bestsellers' not in data:
            return '🔍 아직 수집된 베스트셀러 데이터가 없습니다. 먼저 업데이트를 실행해주세요.'

        previous_data = self.load_data(self.previous_data_file)
        lines = [
            '📋 현재 추적 브랜드 요약',
            f"수집 시간: {data.get('timestamp', 'N/A')}",
            '────────────────────────',
        ]
        brands = self.state.get('brands', [])
        if not brands:
            lines.append('추적 중인 브랜드가 없습니다. /add <브랜드> 로 브랜드를 추가하세요.')
            return '\n'.join(lines)

        for brand in brands:
            brand_lower = brand.lower()
            matches = [item for item in data['bestsellers'] if brand_lower in item['title'].lower()]
            if not matches:
                lines.append(f"❌ {brand}: 현재 목록에 없음")
                continue

            lines.append(f"📌 {brand} ({len(matches)}개)")
            for item in sorted(matches, key=lambda x: x['rank']):
                old_item = self._find_previous_item(previous_data, item)
                diff_text = self._format_rank_diff(old_item, item['rank'])
                lines.append(f"{item['rank']}위 {item['title']} {diff_text}".strip())
                if item.get('image'):
                    lines.append(item['image'])
            lines.append('')

        lines.append('────────────────────────')
        lines.append(f"총 추적 브랜드: {len(brands)}개")
        lines.append('목록 보기: /list    요약 보기: /summary')
        return '\n'.join(lines)

    def build_movers_summary_text(self):
        data = self.load_data(self.movers_data_file)
        if not data or 'movers' not in data:
            return '🔍 아직 수집된 Move and Shakers 데이터가 없습니다. 먼저 업데이트를 실행해주세요.'

        previous_data = self.load_data(self.previous_movers_data_file)
        lines = [
            '📋 현재 추적 브랜드 Move and Shakers 요약',
            f"수집 시간: {data.get('timestamp', 'N/A')}",
            '────────────────────────',
        ]
        brands = self.state.get('brands', [])
        if not brands:
            lines.append('추적 중인 브랜드가 없습니다. /add <브랜드> 로 브랜드를 추가하세요.')
            return '\n'.join(lines)

        for brand in brands:
            brand_lower = brand.lower()
            matches = [item for item in data['movers'] if brand_lower in item['title'].lower()]
            if not matches:
                lines.append(f"❌ {brand}: 현재 목록에 없음")
                continue

            lines.append(f"📌 {brand} ({len(matches)}개)")
            for item in sorted(matches, key=lambda x: x['rank']):
                old_item = self._find_previous_item(previous_data, item, data_key='movers')
                diff_text = self._format_rank_diff(old_item, item['rank'])
                lines.append(f"{item['rank']}위 {item['title']} {diff_text}".strip())
                if item.get('image'):
                    lines.append(item['image'])
            lines.append('')

        lines.append('────────────────────────')
        lines.append(f"총 추적 브랜드: {len(brands)}개")
        lines.append('목록 보기: /list    요약 보기: /summary')
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

    def send_telegram_photo(self, chat_id, image_path, caption=None):
        token = self.get_telegram_token()
        if not token:
            logging.warning('Telegram bot token is not configured. Skipping Telegram photo notification.')
            return False

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        data = {'chat_id': chat_id}
        if caption:
            data['caption'] = caption
        try:
            with open(image_path, 'rb') as photo_file:
                files = {'photo': photo_file}
                response = requests.post(url, data=data, files=files, timeout=20)
            if response.status_code != 200:
                logging.error(f"Telegram photo error: {response.status_code} {response.text}")
                return False
            return True
        except requests.RequestException as e:
            logging.error(f"Error sending Telegram photo: {e}")
            return False
        except FileNotFoundError:
            logging.error(f"Telegram photo error: file not found {image_path}")
            return False

    def broadcast_photo(self, image_path, caption=None):
        if not self.get_telegram_token():
            logging.warning('Telegram bot token missing. Broadcast photo skipped.')
            return

        for chat_id in self.state.get('chat_ids', []):
            self.send_telegram_photo(chat_id, image_path, caption)

    def _get_font(self, size=24):
        font_paths = [
            '/System/Library/Fonts/AppleGothic.ttf',
            '/System/Library/Fonts/AppleSDGothicNeo.ttc',
            '/Library/Fonts/AppleGothic.ttf',
            '/Library/Fonts/AppleSDGothicNeo.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]
        for path in font_paths:
            try:
                if Path(path).exists():
                    return ImageFont.truetype(str(path), size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _fetch_remote_image(self, url, size=(320, 320)):
        if not url:
            return None
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert('RGB')
            image.thumbnail(size, Image.Resampling.LANCZOS)
            return image
        except Exception:
            return None

    def _wrap_text(self, text, font, max_width, draw):
        words = text.split()
        lines = []
        current_line = ''
        for word in words:
            candidate = f"{current_line} {word}".strip()
            width = draw.textlength(candidate, font=font)
            if width <= max_width:
                current_line = candidate
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        return lines

    def _truncate_text(self, text, max_chars=20):
        if not text:
            return ''
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 1].rstrip() + '…'

    def _build_image_card(self, headline, items_by_brand, footer_text=None):
        # Mobile-first Telegram card layout (1080px wide portrait image).
        width = 1080
        padding = 44
        section_gap = 24
        item_gap = 16
        header_height = 170
        brand_header_height = 74
        item_row_height = 302
        thumb_size = 264

        title_font = self._get_font(56)
        subtitle_font = self._get_font(28)
        brand_font = self._get_font(42)
        item_title_font = self._get_font(34)
        item_meta_font = self._get_font(28)
        small_font = self._get_font(30)

        total_height = padding + header_height
        for entry in items_by_brand:
            section_height = brand_header_height + 26
            if entry['items']:
                section_height += len(entry['items']) * item_row_height + (len(entry['items']) - 1) * item_gap
            else:
                section_height += 72
            total_height += section_height + section_gap

        if footer_text:
            total_height += 72
        total_height += padding

        image = Image.new('RGB', (width, total_height), color=(245, 248, 252))
        draw = ImageDraw.Draw(image)

        # Soft vertical gradient background to improve card contrast on mobile screens.
        for i in range(total_height):
            ratio = i / max(total_height - 1, 1)
            r = int(245 - ratio * 14)
            g = int(248 - ratio * 10)
            b = int(252 - ratio * 6)
            draw.line((0, i, width, i), fill=(r, g, b))

        y = padding
        draw.rounded_rectangle(
            (padding, y, width - padding, y + header_height),
            radius=30,
            fill=(255, 255, 255),
            outline=(220, 227, 236),
            width=2,
        )
        draw.text((padding + 28, y + 24), headline, fill=(20, 28, 38), font=title_font)
        draw.text(
            (padding + 28, y + 98),
            datetime.now().strftime('Generated: %Y-%m-%d %H:%M:%S'),
            fill=(104, 116, 134),
            font=subtitle_font,
        )
        y += header_height + section_gap

        for entry in items_by_brand:
            section_top = y
            section_height = brand_header_height + 26
            if entry['items']:
                section_height += len(entry['items']) * item_row_height + (len(entry['items']) - 1) * item_gap
            else:
                section_height += 72

            draw.rounded_rectangle(
                (padding, section_top, width - padding, section_top + section_height),
                radius=30,
                fill=(255, 255, 255),
                outline=(220, 227, 236),
                width=2,
            )
            draw.text(
                (padding + 24, section_top + 18),
                f"{entry['brand']} ({len(entry['items'])})",
                fill=(24, 35, 49),
                font=brand_font,
            )

            row_y = section_top + brand_header_height
            if not entry['items']:
                draw.text((padding + 24, row_y + 10), 'No matched products found.', fill=(119, 129, 144), font=small_font)
                y = section_top + section_height + section_gap
                continue

            for item in entry['items']:
                row_top = row_y
                row_bottom = row_top + item_row_height
                row_left = padding + 18
                row_right = width - padding - 18

                draw.rounded_rectangle(
                    (row_left, row_top, row_right, row_bottom),
                    radius=24,
                    fill=(249, 252, 255),
                    outline=(229, 236, 245),
                    width=1,
                )

                thumb_x = row_left + 16
                thumb_y = row_top + 15
                thumb = self._fetch_remote_image(item.get('image'), size=(thumb_size, thumb_size))
                if thumb:
                    image.paste(thumb, (thumb_x, thumb_y))
                else:
                    draw.rounded_rectangle(
                        (thumb_x, thumb_y, thumb_x + thumb_size, thumb_y + thumb_size),
                        radius=18,
                        fill=(235, 241, 247),
                        outline=(205, 216, 229),
                        width=1,
                    )
                    draw.text((thumb_x + 30, thumb_y + 112), 'No Image', fill=(134, 144, 158), font=small_font)

                text_x = thumb_x + thumb_size + 20
                text_width = row_right - text_x - 16

                title = self._truncate_text(item.get('title', ''), 25)
                title_lines = self._wrap_text(f"#{item['rank']} {title}", item_title_font, text_width, draw)
                draw.text((text_x, row_top + 20), title_lines[0] if title_lines else f"#{item['rank']}", fill=(28, 37, 52), font=item_title_font)
                if len(title_lines) > 1:
                    draw.text((text_x, row_top + 64), title_lines[1], fill=(28, 37, 52), font=item_title_font)

                diff_text = item.get('diff_text', '')
                draw.text((text_x, row_top + 150), diff_text, fill=(67, 98, 132), font=item_meta_font)

                meta_text = f"{item.get('price', 'N/A')}  |  {item.get('rating', 'N/A')}  |  {item.get('reviews', 'N/A')}"
                meta_lines = self._wrap_text(meta_text, small_font, text_width, draw)
                draw.text((text_x, row_top + 196), meta_lines[0] if meta_lines else meta_text, fill=(111, 123, 140), font=small_font)

                row_y += item_row_height + item_gap

            y = section_top + section_height + section_gap

        if footer_text:
            draw.text((padding + 8, y + 4), footer_text, fill=(112, 123, 140), font=small_font)

        output_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        image.save(output_file.name, format='PNG')
        output_file.close()
        return output_file.name

    def build_summary_image(self, data, previous_data):
        brands = self.state.get('brands', [])
        if not brands:
            items_by_brand = [{'brand': 'No tracked brands.', 'items': []}]
        else:
            items_by_brand = []
            for brand in brands:
                brand_lower = brand.lower()
                matches = [item for item in data.get('bestsellers', []) if brand_lower in item['title'].lower()]
                summary_items = []
                for item in sorted(matches, key=lambda x: x['rank'])[:3]:
                    diff_text = self._format_rank_diff_en(self._find_previous_item(previous_data, item), item['rank'])
                    summary_items.append({**item, 'diff_text': diff_text})
                items_by_brand.append({'brand': brand, 'items': summary_items})

        image_path = self._build_image_card('Summary', items_by_brand, footer_text=f'Total tracked brands: {len(brands)}')
        caption = f'Summary • {len(brands)} brands'
        return image_path, caption

    def build_movers_summary_image(self, data, previous_data):
        brands = self.state.get('brands', [])
        if not brands:
            items_by_brand = [{'brand': 'No tracked brands.', 'items': []}]
        else:
            items_by_brand = []
            for brand in brands:
                brand_lower = brand.lower()
                matches = [item for item in data.get('movers', []) if brand_lower in item['title'].lower()]
                summary_items = []
                for item in sorted(matches, key=lambda x: x['rank'])[:3]:
                    diff_text = self._format_rank_diff_en(self._find_previous_item(previous_data, item, data_key='movers'), item['rank'])
                    summary_items.append({**item, 'diff_text': diff_text})
                items_by_brand.append({'brand': brand, 'items': summary_items})

        image_path = self._build_image_card('Move and Shakers Summary', items_by_brand, footer_text=f'Total tracked brands: {len(brands)}')
        caption = f'Move and Shakers Summary • {len(brands)} brands'
        return image_path, caption

    def build_update_image(self, old_data, new_data):
        brands = self.state.get('brands', [])
        if not brands:
            items_by_brand = [{'brand': 'No tracked brands.', 'items': []}]
        else:
            items_by_brand = []
            for brand in brands:
                brand_lower = brand.lower()
                matches = [item for item in new_data.get('bestsellers', []) if brand_lower in item['title'].lower()]
                summary_items = []
                for item in sorted(matches, key=lambda x: x['rank'])[:3]:
                    diff_text = self._format_rank_diff_en(self._find_previous_item(old_data, item), item['rank'])
                    summary_items.append({**item, 'diff_text': diff_text})
                items_by_brand.append({'brand': brand, 'items': summary_items})

        image_path = self._build_image_card('Amazon Beauty Bestseller Update', items_by_brand, footer_text=f'Total tracked brands: {len(brands)}')
        caption = f'Amazon Beauty Bestseller Update • {len(brands)} brands'
        return image_path, caption

    def build_movers_update_image(self, old_data, new_data):
        brands = self.state.get('brands', [])
        if not brands:
            items_by_brand = [{'brand': 'No tracked brands.', 'items': []}]
        else:
            items_by_brand = []
            for brand in brands:
                brand_lower = brand.lower()
                matches = [item for item in new_data.get('movers', []) if brand_lower in item['title'].lower()]
                summary_items = []
                for item in sorted(matches, key=lambda x: x['rank'])[:3]:
                    diff_text = self._format_rank_diff_en(self._find_previous_item(old_data, item, data_key='movers'), item['rank'])
                    summary_items.append({**item, 'diff_text': diff_text})
                items_by_brand.append({'brand': brand, 'items': summary_items})

        image_path = self._build_image_card('Amazon Beauty Move and Shakers Update', items_by_brand, footer_text=f'Total tracked brands: {len(brands)}')
        caption = f'Amazon Beauty Move and Shakers Update • {len(brands)} brands'
        return image_path, caption

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
            has_bestseller = bool(self.load_data())
            has_movers = bool(self.load_data(self.movers_data_file))
            if not has_bestseller or not has_movers:
                if self.fetch_and_save_all_data():
                    return self.send_telegram_message(chat_id, '✅ 알림 등록 및 초기 데이터 수집이 완료되었습니다. 6시간마다 순위 변동을 전송합니다.')
                return self.send_telegram_message(chat_id, '⚠️ 초기 데이터 수집에 실패했습니다. 잠시 후 다시 시도해주세요.')
            return self.send_telegram_message(chat_id, '✅ 알림 등록이 완료되었습니다. 6시간마다 추적 브랜드 순위 변동을 전송합니다.')

        if action in ['add', '추가']:
            if not argument:
                return self.send_telegram_message(chat_id, '사용법: /add <브랜드> 또는 추가 <브랜드>')
            added = self.add_brand(argument)
            has_bestseller = bool(self.load_data())
            has_movers = bool(self.load_data(self.movers_data_file))
            if not has_bestseller or not has_movers:
                if self.fetch_and_save_all_data():
                    pass
                else:
                    return self.send_telegram_message(chat_id, '⚠️ 데이터 수집에 실패했습니다. 잠시 후 다시 시도해주세요.')
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

            sent_any = False
            data = self.load_data()
            previous_data = self.load_data(self.previous_data_file)
            if data and data.get('bestsellers'):
                image_path, caption = self.build_update_image(previous_data, data)
                sent = self.send_telegram_photo(chat_id, image_path, caption)
                try:
                    os.remove(image_path)
                except Exception:
                    pass
                if sent:
                    sent_any = True

            movers_data = self.load_data(self.movers_data_file)
            previous_movers_data = self.load_data(self.previous_movers_data_file)
            if movers_data and movers_data.get('movers'):
                image_path, caption = self.build_movers_update_image(previous_movers_data, movers_data)
                sent = self.send_telegram_photo(chat_id, image_path, caption)
                try:
                    os.remove(image_path)
                except Exception:
                    pass
                if sent:
                    sent_any = True

            if sent_any:
                return True
            return self.send_telegram_message(chat_id, '🔄 즉시 업데이트를 완료했습니다. 추적 중인 브랜드 순위 변동을 전송했습니다.')

        if action in ['summary', '요약']:
            data = self.load_data()
            if not data or not data.get('bestsellers'):
                if not self.fetch_and_save_data():
                    return self.send_telegram_message(chat_id, '🔍 데이터 업데이트에 실패했습니다. 잠시 후 다시 시도해주세요.')
                data = self.load_data()

            movers_data = self.load_data(self.movers_data_file)
            if not movers_data or not movers_data.get('movers'):
                if not self.fetch_and_save_movers_data():
                    return self.send_telegram_message(chat_id, '🔍 Move and Shakers 데이터 업데이트에 실패했습니다. 잠시 후 다시 시도해주세요.')
                movers_data = self.load_data(self.movers_data_file)

            sent_any = False

            previous_data = self.load_data(self.previous_data_file)
            image_path, caption = self.build_summary_image(data, previous_data)
            sent = self.send_telegram_photo(chat_id, image_path, caption)
            try:
                os.remove(image_path)
            except Exception:
                pass
            if sent:
                sent_any = True

            previous_movers_data = self.load_data(self.previous_movers_data_file)
            image_path, caption = self.build_movers_summary_image(movers_data, previous_movers_data)
            sent = self.send_telegram_photo(chat_id, image_path, caption)
            try:
                os.remove(image_path)
            except Exception:
                pass
            if sent:
                sent_any = True

            if sent_any:
                return True

            text_summary = self.build_summary_text()
            movers_summary = self.build_movers_summary_text()
            return self.send_telegram_message(chat_id, f'{text_summary}\n\n{movers_summary}')

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

        logging.info('No bestseller data available. Fetching initial data now...')
        if self.fetch_and_save_data():
            return self.load_data()
        return None

    def fetch_and_save_data(self):
        bestsellers = self.fetch_bestsellers()
        if not bestsellers:
            return False
        return self.save_data(bestsellers)

    def fetch_and_save_movers_data(self):
        movers = self.fetch_movers_and_shakers()
        if not movers:
            return False
        return self.save_data(
            movers,
            path=self.movers_data_file,
            previous_path=self.previous_movers_data_file,
            data_key='movers',
        )

    def fetch_and_save_all_data(self):
        bestsellers_saved = self.fetch_and_save_data()
        movers_saved = self.fetch_and_save_movers_data()
        return bestsellers_saved or movers_saved

    def update(self):
        logging.info('Updating bestseller and movers-and-shakers data...')

        previous_bestseller_data = self.load_data(self.data_file)
        bestsellers = self.fetch_bestsellers()
        bestseller_saved = False
        if bestsellers:
            bestseller_saved = self.save_data(bestsellers)
            if bestseller_saved:
                logging.info(f'Successfully updated bestsellers with {len(bestsellers)} products')
        else:
            logging.warning('Failed to update bestseller data')

        previous_movers_data = self.load_data(self.movers_data_file)
        movers = self.fetch_movers_and_shakers()
        movers_saved = False
        if movers:
            movers_saved = self.save_data(
                movers,
                path=self.movers_data_file,
                previous_path=self.previous_movers_data_file,
                data_key='movers',
            )
            if movers_saved:
                logging.info(f'Successfully updated movers and shakers with {len(movers)} products')
        else:
            logging.warning('Failed to update movers and shakers data')

        if self.state.get('chat_ids'):
            if bestseller_saved:
                image_path, caption = self.build_update_image(
                    previous_bestseller_data,
                    {'timestamp': datetime.now().isoformat(), 'bestsellers': bestsellers},
                )
                self.broadcast_photo(image_path, caption)
                try:
                    os.remove(image_path)
                except Exception:
                    pass

            if movers_saved:
                image_path, caption = self.build_movers_update_image(
                    previous_movers_data,
                    {'timestamp': datetime.now().isoformat(), 'movers': movers},
                )
                self.broadcast_photo(image_path, caption)
                try:
                    os.remove(image_path)
                except Exception:
                    pass

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
    parser.add_argument('--use-browser', action='store_true', help='Use Selenium browser automation to load dynamic items')
    return parser.parse_args()


def run_ci_mode(tracker, args):
    chat_id = args.telegram_chat_id or os.getenv('TELEGRAM_CHAT_ID')
    text = args.telegram_text or os.getenv('TELEGRAM_TEXT')

    if args.use_browser:
        tracker.config['use_browser'] = True
    elif os.getenv('CI', '').lower() in {'1', 'true', 'yes'} and tracker._is_playwright_available():
        # In CI, prefer browser mode for full 50+50 item capture after lazy loading.
        tracker.config['use_browser'] = True

    logging.info(f'CI payload chat_id={chat_id!r}, text={text!r}, update_now={args.update_now}, serve={args.serve}, use_browser={tracker.config.get("use_browser")}')

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
