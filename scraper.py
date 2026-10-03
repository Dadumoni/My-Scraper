from io import BytesIO
import os
import re
import time
import urllib.parse
import boto3
from botocore.exceptions import BotoCoreError
from dotenv import load_dotenv
import numpy as np
from pymongo import MongoClient
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter
from supabase import create_client

# ---------------------------------------------------------------------------
# 1. Environment & Configurations Setup
# ---------------------------------------------------------------------------
load_dotenv()

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "https://p4455.com")
START_PAGE_URL = f"{BASE_DOMAIN}/latest"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "scraper_db")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "processed_posts")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "posts")

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_CUSTOM_DOMAIN = os.getenv("R2_PUBLIC_CUSTOM_DOMAIN")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# 2. External Clients Initialization
# ---------------------------------------------------------------------------
# MongoDB Initialization
mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
mongo_col = mongo_db[MONGO_COLLECTION]

# Supabase Initialization
supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Cloudflare R2 Initialization (S3 Compatible API)
r2_s3_client = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)

# ---------------------------------------------------------------------------
# 3. Helper Functions
# ---------------------------------------------------------------------------


def clean_title(raw_title):
    """Removes '– Mydesi.net' or '- Mydesi.net' suffix from post titles."""
    if not raw_title:
        return "Untitled"

    cleaned = re.sub(
        r"[\s\-\–|]*mydesi\.net.*$", "", raw_title, flags=re.IGNORECASE
    )
    cleaned = cleaned.strip()

    return cleaned if cleaned else raw_title.strip()


def upload_thumbnail_to_r2(image_bytes, r2_path):
    """
    Uploads PIL image bytes to a specified folder path in the Cloudflare R2 bucket
    (e.g., thumbnails/filename.jpg) and returns the public URL.
    """
    try:
        r2_s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=r2_path,
            Body=image_bytes,
            ContentType="image/jpeg",
        )
        public_url = f"{R2_PUBLIC_CUSTOM_DOMAIN.rstrip('/')}/{r2_path}"
        return public_url
    except BotoCoreError as e:
        print(f"  [!] Cloudflare R2 Upload Error: {e}")
        return None


# ---------------------------------------------------------------------------
# 4. Web Extraction Logic
# ---------------------------------------------------------------------------


def fetch_soup(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            return BeautifulSoup(resp.text, "html.parser"), resp.text
    except requests.RequestException as e:
        print(f"  [!] Failed to load {url}: {e}")
    return None, ""


def extract_post_links_from_page(page_url):
    soup, _ = fetch_soup(page_url)
    if not soup:
        return []

    post_urls = []
    seen = set()

    video_blocks = soup.find_all("div", class_="video-block")

    for block in video_blocks:
        a_tag = block.find("a", href=True)
        if a_tag:
            href = a_tag["href"]
            full_url = urllib.parse.urljoin(BASE_DOMAIN, href)

            if full_url not in seen:
                seen.add(full_url)
                post_urls.append(full_url)

    return post_urls


def pick_video_links(mp4_list, limit=3):
    """Max 3 links: pehla video_url, doosra video_url1, teesra video_url2.
    _480p wale duplicate quality links tabhi lete hain jab aur koi link na ho."""
    main_links = [link for link in mp4_list if "_480p" not in link.lower()]
    return (main_links or mp4_list)[:limit]


def extract_media_from_post(url):
    soup, html = fetch_soup(url)
    if not soup:
        return None, [], [], None

    # Title Extraction & Cleaning
    raw_title = "Untitled"
    if soup.title and soup.title.string:
        raw_title = soup.title.string.strip()

    title = clean_title(raw_title)

    # MP4 Links
    mp4_list = []
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src")
        if src and ".mp4" in src.lower():
            full_link = urllib.parse.urljoin(url, src)
            if full_link not in mp4_list:
                mp4_list.append(full_link)

    regex_mp4 = r'https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?'
    for match in re.findall(regex_mp4, html, re.IGNORECASE):
        if match not in mp4_list:
            mp4_list.append(match)

    video_links = pick_video_links(mp4_list)

    # Video ID
    video_id = None
    for m_link in mp4_list:
        match = re.search(r"/(\d+)(?:_\w+)?\.mp4", m_link)
        if match:
            video_id = match.group(1)
            break

    # Frames (1..10)
    frame_urls = []
    if video_id:
        frame_pattern = re.compile(
            rf"https?://[^\s\"'<>]*/{video_id}/frame_\d+\.jpg(?:\?[^\s\"'<>]*)?",
            re.IGNORECASE,
        )
        found_frames = frame_pattern.findall(html)

        base_pview_url = None
        if found_frames:
            sample_frame = found_frames[0]
            base_pview_url = sample_frame.split(f"/{video_id}/")[0]
        else:
            pview_match = re.search(
                rf'(https?://[^\s"\'<>]+)/{video_id}/frame_', html
            )
            if pview_match:
                base_pview_url = pview_match.group(1)
            elif mp4_list:
                base_pview_url = mp4_list[0].rsplit("/", 1)[0]

        if base_pview_url:
            for i in range(1, 11):
                frame_urls.append(f"{base_pview_url}/{video_id}/frame_{i}.jpg")
        elif found_frames:
            frame_urls = sorted(list(set(found_frames)))[:10]

    return title, video_links, frame_urls, video_id


# ---------------------------------------------------------------------------
# 5. Image Processing (Grid Generation in Memory)
# ---------------------------------------------------------------------------


def is_9by16(img, tol=0.02):
    return abs((img.width / img.height) - (9 / 16)) < tol


def trim_black_bars(img, threshold=24, min_ratio=0.03):
    gray = np.array(img.convert("L"))
    mask = gray > threshold

    rows = np.where(mask.mean(axis=1) > min_ratio)[0]
    cols = np.where(mask.mean(axis=0) > min_ratio)[0]

    if len(rows) == 0 or len(cols) == 0:
        return img

    top, bottom = rows[0], rows[-1] + 1
    left, right = cols[0], cols[-1] + 1

    if (right - left) < img.width * 0.1 or (bottom - top) < img.height * 0.1:
        return img

    return img.crop((left, top, right, bottom))


def cover_resize(img, cell_w, cell_h):
    img_ratio = img.width / img.height
    target_ratio = cell_w / cell_h
    if img_ratio > target_ratio:
        new_h = cell_h
        new_w = int(cell_h * img_ratio)
    else:
        new_w = cell_w
        new_h = int(cell_w / img_ratio)
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - cell_w) // 2
    top = (new_h - cell_h) // 2
    return resized.crop((left, top, left + cell_w, top + cell_h))


def fit_width_blur_tb(img, cell_w, cell_h, blur_radius=30):
    new_w = cell_w
    new_h = int(round(cell_w * img.height / img.width))

    if new_h >= cell_h:
        return cover_resize(img, cell_w, cell_h)

    bg = cover_resize(img, cell_w, cell_h).filter(
        ImageFilter.GaussianBlur(blur_radius)
    )
    fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    bg.paste(fg, (0, (cell_h - new_h) // 2))
    return bg


def is_16by9(img, tol=0.05):
    """True 16:9 landscape content. 16:9 canvas ke andar blur/padding ke saath
    rakhi 9:16 image ko landscape nahi maanta."""
    if abs((img.width / img.height) - (16 / 9)) >= tol:
        return False

    gray = np.array(img.convert("L").resize((320, 180)), dtype=np.float32)
    detail = np.abs(np.diff(gray, axis=1))
    center = detail[:, 120:200].mean()
    left = detail[:, :48].mean()
    right = detail[:, -48:].mean()

    # Dono side blur/flat hain aur beech me detail hai => portrait content
    if center > 0 and max(left, right) < 0.4 * center:
        return False
    return True


def build_landscape_grid_bytes(images, cols, rows):
    canvas_width, canvas_height = 1920, 1080
    cell_w, cell_h = canvas_width // cols, canvas_height // rows

    grid_canvas = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))

    for idx, img in enumerate(images[: cols * rows]):
        cell_img = fit_width_blur_tb(img, cell_w, cell_h)
        row, col = idx // cols, idx % cols
        grid_canvas.paste(cell_img, (col * cell_w, row * cell_h))

    output_buffer = BytesIO()
    grid_canvas.save(output_buffer, format="JPEG", quality=95)
    return output_buffer.getvalue()


def create_16by9_grid_bytes(image_urls):
    if not image_urls:
        return None

    images = []
    for url in image_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code == 200:
                img = Image.open(BytesIO(resp.content)).convert("RGB")
                images.append(img)
            else:
                alt_url = re.sub(
                    r"/frame_(\d+)\.jpg",
                    lambda m: f"/frame_{int(m.group(1)):02d}.jpg",
                    url,
                )
                if alt_url != url:
                    alt_resp = requests.get(alt_url, headers=HEADERS, timeout=8)
                    if alt_resp.status_code == 200:
                        img = Image.open(BytesIO(alt_resp.content)).convert(
                            "RGB"
                        )
                        images.append(img)
        except Exception:
            pass

    if not images:
        return None

    # 16:9 images zyada hon to unka alag grid (9 ya 6 images)
    landscape = [t for t in (trim_black_bars(i) for i in images) if is_16by9(t)]
    if len(landscape) >= 9:
        return build_landscape_grid_bytes(landscape, cols=3, rows=3)
    if len(landscape) >= 6:
        return build_landscape_grid_bytes(landscape, cols=3, rows=2)

    canvas_width, canvas_height = 1920, 1080
    cell_w, cell_h = canvas_width // 5, canvas_height // 2

    grid_canvas = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))

    for idx, img in enumerate(images[:10]):
        content = trim_black_bars(img)

        if is_9by16(content):
            cell_img = cover_resize(content, cell_w, cell_h)
        else:
            cell_img = fit_width_blur_tb(content, cell_w, cell_h)

        row, col = idx // 5, idx % 5
        grid_canvas.paste(cell_img, (col * cell_w, row * cell_h))

    output_buffer = BytesIO()
    grid_canvas.save(output_buffer, format="JPEG", quality=95)
    return output_buffer.getvalue()


# ---------------------------------------------------------------------------
# 6. Crawler Orchestrator Loop
# ---------------------------------------------------------------------------


def run_crawler():
    page_num = 1
    total_processed_posts = 0

    print("==================================================")
    print(f" Starting Pipeline for Domain: {BASE_DOMAIN}")
    print("==================================================\n")

    while True:
        if page_num == 1:
            current_page_url = START_PAGE_URL
        else:
            current_page_url = f"{BASE_DOMAIN}/latest/page/{page_num}/"

        print(f"\n--- [PAGE {page_num}] Fetching: {current_page_url} ---")

        post_urls = extract_post_links_from_page(current_page_url)

        if not post_urls:
            print(
                f"\n[!] No posts found on Page {page_num}. Reached end of pagination."
            )
            break

        print(f"Found {len(post_urls)} post links on Page {page_num}.")

        for idx, post_url in enumerate(post_urls, start=1):
            print(f"\n({idx}/{len(post_urls)}) Checking Post: {post_url}")

            # 1. MongoDB Check (Duplicate filtering)
            if mongo_col.find_one({"post_url": post_url}):
                print("  [=>] Post already processed (MongoDB Match). Skipping...")
                continue

            # 2. Extract Details
            (
                title,
                video_links,
                frame_urls,
                video_id,
            ) = extract_media_from_post(post_url)

            if not video_links:
                print("  [-] No valid MP4 link found. Skipping...")
                continue

            print(f"  [+] Cleaned Title: {title}")
            for n, link in enumerate(video_links, start=1):
                print(f"  [+] Video link {n}: {link}")

            # 3. Create Grid & Upload to Cloudflare R2 (Inside 'thumbnails/' folder)
            thumbnail_url = None
            if frame_urls:
                thumb_bytes = create_16by9_grid_bytes(frame_urls)
                if thumb_bytes:
                    filename = (
                        f"{video_id}_thumb.jpg"
                        if video_id
                        else f"thumb_{int(time.time())}.jpg"
                    )

                    r2_path = f"thumbnails/{filename}"

                    thumbnail_url = upload_thumbnail_to_r2(thumb_bytes, r2_path)
                    if thumbnail_url:
                        print(f"  [+] Thumbnail R2 Uploaded: {thumbnail_url}")
            else:
                print("  [-] No frame URLs available.")

            # 4. Insert into Supabase
            supabase_data = {
                "title": title,
                "video_url": video_links[0],
                "video_url1": video_links[1] if len(video_links) > 1 else None,
                "video_url2": video_links[2] if len(video_links) > 2 else None,
                "thumbnail_url": thumbnail_url,
                "views": 0,
            }

            try:
                supabase_client.table(SUPABASE_TABLE).insert(
                    supabase_data
                ).execute()
                print("  [+] Inserted successfully into Supabase.")
            except Exception as e:
                print(f"  [!] Supabase Insert Failed: {e}")
                continue

            # 5. Mark as Completed in MongoDB
            mongo_col.insert_one(
                {
                    "post_url": post_url,
                    "video_id": video_id,
                    "scraped_at": time.time(),
                }
            )
            print("  [+] Post marked as processed in MongoDB.")

            total_processed_posts += 1
            time.sleep(0.5)

        page_num += 1
        time.sleep(1)

    print("\n==================================================")
    print(
        f" Pipeline Completed! Total New Posts Processed: {total_processed_posts}"
    )
    print("==================================================")


if __name__ == "__main__":
    run_crawler()
