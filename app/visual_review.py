"""生成编号图片总览，并管理多轮补图与材料覆盖。"""

import base64
import io
import logging
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)


def visual_config(config):
    """校验容器外的图片轮次、总览尺寸及识别关键词配置。"""
    value = config["visual_review"]
    limits = {"max_rounds": 10, "first_round_images": 20, "next_round_images": 20,
              "overview_size": 12, "thumbnail_width": 800, "thumbnail_height": 800,
              "detail_tile_size": 2400, "detail_overlap": 500, "max_detail_parts": 20, "long_image_ratio": 10}
    for key, maximum in limits.items():
        if type(value.get(key)) is not int or not 0 < value[key] <= maximum:
            raise ValueError(f"图片配置 {key} 超出有效范围")
    if value["detail_overlap"] >= value["detail_tile_size"]:
        raise ValueError("图片切片重叠必须小于切片尺寸")
    for key in ("instruction_keywords", "image_reference_keywords", "business_keywords"):
        if not isinstance(value.get(key), list) or any(not isinstance(x, str) or not x for x in value[key]):
            raise ValueError(f"图片关键词配置 {key} 无效")
    return value


def image_message(picture):
    """将图片编码为模型支持的 JPEG 消息。"""
    output = io.BytesIO()
    picture.save(output, format="JPEG", quality=85)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")}}


def detail_images(picture, config):
    """功能说明：按长边切分原始图片，保留小字可读性。参数 picture：已校正方向的 RGB 图片；config：图片处理配置。返回值：按阅读顺序排列的图片消息。"""
    size, overlap = config["detail_tile_size"], config["detail_overlap"]
    width, height = picture.size
    if max(width, height) / min(width, height) <= config["long_image_ratio"]:
        return [image_message(ImageOps.contain(picture, (min(size, width), min(size, height))))]
    scale = min(1, size / min(width, height))
    picture = picture.resize((max(1, round(width * scale)), max(1, round(height * scale))))
    width, height = picture.size
    length = max(width, height)
    starts = [0]
    while starts[-1] + size < length:
        starts.append(starts[-1] + size - overlap)
    if len(starts) > config["max_detail_parts"]:
        raise ValueError("长图切片超过上限，请拆分关键说明")
    result = []
    for start in starts:
        box = (0, start, width, min(start + size, height)) if height >= width else (start, 0, min(start + size, width), height)
        result.append(image_message(picture.crop(box)))
    return result


class VisualReview:
    """每次评级独立维护总览、原图切片、已发送材料及必读图片。"""

    def __init__(self, images, sources, config, parts=None, demand=None):
        """功能说明：构造完整图片目录及总览。参数 images：单图预览；sources：图片来源与上下文；config：轮次配置；parts：可选原图切片；demand：需求文字用于检查看图要求。返回值：无。"""
        self.config = config
        self.assets, self.sent, self.required = {}, set(), set()
        self.catalog, self.overviews, self.original_keys = [], [], {}
        self.queue = []
        groups = defaultdict(list)
        for index, image in enumerate(images, 1):
            source = dict(sources[index - 1]) if index <= len(sources) else {"source": f"附件图片 {index}"}
            source["image_index"] = index
            group = source.get("group") or source["source"].rsplit(" / ", 1)[0]
            source["group"] = group
            self.catalog.append(source)
            groups[group].append(index)
            fragments = parts[index - 1] if parts and index <= len(parts) else [image]
            keys = []
            for number, fragment in enumerate(fragments, 1):
                key = f"image:{index}:{number}"
                self.assets[key] = fragment
                keys.append(key)
            self.original_keys[index] = keys
            if source.get("requires_detail"):
                self.required.add(index)
        # 正文要求看图却没有明确定位时，按保守范围检查全部图片。
        text = " ".join(str((demand or {}).get(key) or "") for key in ("title", "description", "notes"))
        if any(word in text for word in config["image_reference_keywords"]):
            self.required.update(self.original_keys)
        grouped = []
        def priority(indices):
            if any(index in self.required for index in indices):
                return 0
            return 2 if all(any(word in self.catalog[index - 1]["source"] + self.catalog[index - 1].get("context", "") for word in config["business_keywords"]) for index in indices) else 1
        for group, indices in sorted(groups.items(), key=lambda item: priority(item[1])):
            chunks = []
            for offset in range(0, len(indices), config["overview_size"]):
                members = indices[offset:offset + config["overview_size"]]
                number = len(self.overviews) + 1
                self.overviews.append({"overview_id": number, "group": group, "image_indices": members})
                key = f"overview:{number}"
                self.assets[key] = self._overview(images, members)
                chunks.append(key)
            grouped.append(chunks)
        # 轮转各工作表，首轮尽量覆盖不同来源。
        self.overview_order = [items[index] for index in range(max(map(len, grouped), default=0)) for items in grouped if index < len(items)]

    def _overview(self, images, indices):
        width, height = self.config["thumbnail_width"], self.config["thumbnail_height"]
        columns = min(2, len(indices))
        canvas = Image.new("RGB", (width * columns, (height + 38) * ((len(indices) + columns - 1) // columns)), "white")
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=24)
        for slot, index in enumerate(indices):
            data = base64.b64decode(images[index - 1]["image_url"]["url"].split(",", 1)[1])
            with Image.open(io.BytesIO(data)) as picture:
                thumbnail = ImageOps.contain(picture, (width - 16, height - 16))
                x, y = (slot % columns) * width, (slot // columns) * (height + 38)
                canvas.paste(thumbnail, (x + (width - thumbnail.width) // 2, y + 38 + (height - thumbnail.height) // 2))
                draw.text((x + 10, y + 5), f"IMG {index}", font=font, fill="black")
        return image_message(canvas)

    def pending(self):
        """返回仍需发送的指定原图、关键说明切片与总览。"""
        mandatory = [key for index in sorted(self.required) for key in self.original_keys[index]]
        return list(dict.fromkeys(key for key in self.queue + mandatory + self.overview_order if key not in self.sent))

    def take(self, first=False):
        limit = self.config["first_round_images" if first else "next_round_images"]
        keys = [key for key in self.overview_order if key not in self.sent][:limit] if first else self.pending()[:limit]
        self.sent.update(keys)
        return keys, [self.assets[key] for key in keys]

    def request(self, requests):
        """校验模型点选的图片编号，并将全部请求保留至发送完成。"""
        if not isinstance(requests, list) or not requests or len(requests) > len(self.catalog) + len(self.overviews):
            raise ValueError("AI 补图请求数量无效")
        selected = []
        for item in requests:
            if not isinstance(item, dict) or type(item.get("id")) is not int or not isinstance(item.get("reason"), str) or not item["reason"].strip() or len(item["reason"]) > 500:
                raise ValueError("AI 补图请求格式无效")
            if item.get("kind") == "image" and item["id"] in self.original_keys:
                self.required.add(item["id"])
                selected.extend(self.original_keys[item["id"]])
            elif item.get("kind") == "overview" and 1 <= item["id"] <= len(self.overviews):
                selected.append(f"overview:{item['id']}")
            else:
                raise ValueError("AI 请求了不存在的图片")
        self.queue = list(dict.fromkeys(self.queue + selected))

    def state(self, round_number, keys):
        """生成模型可见的阅读状态，编号与本轮图片顺序严格一致。"""
        return {"round": round_number, "max_rounds": self.config["max_rounds"], "current_images": keys,
                "sent_images": sorted(self.sent), "pending_required": self.pending(),
                "image_catalog": self.catalog, "overview_catalog": self.overviews,
                "next_round_limit": self.config["next_round_images"]}
