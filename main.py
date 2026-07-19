import os
import asyncio
from io import BytesIO
from PIL import Image, ImageFilter, ImageEnhance
import imagehash
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp

class KardsGuessPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 插件数据存储路径，用于存放卡牌图片库
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_kards_guess")
        self.cards_dir = os.path.join(self.data_dir, "cards")
        self.hash_dir = os.path.join(self.data_dir, "hashes")
        # 确保目录存在
        os.makedirs(self.cards_dir, exist_ok=True)
        os.makedirs(self.hash_dir, exist_ok=True)
        # 存储所有卡牌的哈希值 {卡牌名: 哈希值列表}
        self.card_hashes = {}
        # 加载卡牌库
        self.load_card_database()

    def load_card_database(self):
        """加载卡牌图片库，计算并缓存每张卡牌的感知哈希"""
        self.card_hashes.clear()
        if not os.path.exists(self.cards_dir):
            logger.warning("卡牌图片目录不存在，请将卡牌图片放入 data/plugins/astrbot_plugin_kards_guess/cards/")
            return
        
        for filename in os.listdir(self.cards_dir):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                card_name = os.path.splitext(filename)[0]
                img_path = os.path.join(self.cards_dir, filename)
                try:
                    # 计算感知哈希，对缩放、亮度等有一定鲁棒性
                    img = Image.open(img_path).convert('RGB')
                    # 使用更稳健的感知哈希
                    phash = imagehash.phash(img, hash_size=16)
                    
                    # 同时计算多种哈希以提高匹配准确率
                    ahash = imagehash.average_hash(img, hash_size=16)
                    dhash = imagehash.dhash(img, hash_size=16)
                    
                    # 存储多种哈希值
                    self.card_hashes[card_name] = {
                        'phash': phash,
                        'ahash': ahash,
                        'dhash': dhash
                    }
                    logger.info(f"已加载卡牌: {card_name}")
                except Exception as e:
                    logger.error(f"加载卡牌 {filename} 失败: {e}")
        
        logger.info(f"卡牌数据库加载完成，共 {len(self.card_hashes)} 张卡牌")

    def apply_image_effect(self, img: Image.Image, effect: str) -> Image.Image:
        """对图片应用指定的效果"""
        img_copy = img.copy()
        
        if effect == "反色":
            # 反色处理
            return Image.eval(img_copy, lambda x: 255 - x)
        elif effect == "放大":
            # 放大2倍后缩回原大小
            w, h = img_copy.size
            return img_copy.resize((w*2, h*2)).resize((w, h), Image.Resampling.LANCZOS)
        elif effect == "马赛克":
            # 使用像素化模拟马赛克效果
            w, h = img_copy.size
            # 缩小到1/10再放大回来
            small = img_copy.resize((w//10, h//10), Image.Resampling.NEAREST)
            return small.resize((w, h), Image.Resampling.NEAREST)
        elif effect == "模糊":
            return img_copy.filter(ImageFilter.GaussianBlur(radius=3))
        elif effect == "锐化":
            return img_copy.filter(ImageFilter.SHARPEN)
        elif effect == "灰度":
            return img_copy.convert('L').convert('RGB')
        elif effect == "亮度+":
            enhancer = ImageEnhance.Brightness(img_copy)
            return enhancer.enhance(1.5)
        elif effect == "亮度-":
            enhancer = ImageEnhance.Brightness(img_copy)
            return enhancer.enhance(0.5)
        elif effect == "对比度+":
            enhancer = ImageEnhance.Contrast(img_copy)
            return enhancer.enhance(1.5)
        elif effect == "对比度-":
            enhancer = ImageEnhance.Contrast(img_copy)
            return enhancer.enhance(0.5)
        else:
            return img_copy

    def find_best_match(self, query_img: Image.Image, effect: str = None) -> tuple:
        """在卡牌库中查找最匹配的卡牌"""
        # 如果指定了效果，先对查询图片应用效果
        if effect:
            query_img = self.apply_image_effect(query_img, effect)
        
        # 计算查询图片的哈希值
        query_phash = imagehash.phash(query_img, hash_size=16)
        query_ahash = imagehash.average_hash(query_img, hash_size=16)
        query_dhash = imagehash.dhash(query_img, hash_size=16)
        
        best_match = None
        best_score = float('inf')  # 汉明距离越小越相似
        
        for card_name, hashes in self.card_hashes.items():
            # 计算三种哈希的汉明距离，加权求和
            phash_dist = query_phash - hashes['phash']
            ahash_dist = query_ahash - hashes['ahash']
            dhash_dist = query_dhash - hashes['dhash']
            
            # 综合得分（权重可以根据实际情况调整）
            total_dist = phash_dist * 0.5 + ahash_dist * 0.25 + dhash_dist * 0.25
            
            if total_dist < best_score:
                best_score = total_dist
                best_match = card_name
        
        # 设定阈值，如果距离太大则认为是未知卡牌
        # 对于16x16的哈希，最大距离为256，阈值设为30左右比较合适
        if best_score > 30:
            return None, best_score
        
        return best_match, best_score

    @filter.command("猜卡牌")
    async def guess_card(self, event: AstrMessageEvent):
        '''猜卡牌指令：发送一张卡牌图片（可附带效果），机器人猜测是哪张卡牌
        用法：/猜卡牌 [效果名]
        支持的效果：反色、放大、马赛克、模糊、锐化、灰度、亮度+、亮度-、对比度+、对比度-
        示例：/猜卡牌 反色'''
        
        # 检查是否有图片
        if not event.message_obj or not hasattr(event.message_obj, 'message'):
            yield event.plain_result("请发送一张卡牌图片！")
            return
        
        # 提取图片
        image_segments = [seg for seg in event.message_obj.message if isinstance(seg, Comp.Image)]
        if not image_segments:
            yield event.plain_result("未检测到图片，请发送一张卡牌图片！")
            return
        
        # 解析命令参数，获取效果名
        effect = None
        if event.message_str:
            parts = event.message_str.strip().split()
            if len(parts) > 1:
                effect = parts[1]  # 第二个参数为效果名
        
        # 支持的效效果列表
        valid_effects = ['反色', '放大', '马赛克', '模糊', '锐化', '灰度', '亮度+', '亮度-', '对比度+', '对比度-']
        if effect and effect not in valid_effects:
            yield event.plain_result(f"不支持的效果: {effect}\n支持的效果: {', '.join(valid_effects)}")
            return
        
        # 下载图片
        try:
            # 获取第一张图片的URL并下载
            img_url = image_segments[0].url
            if not img_url:
                yield event.plain_result("无法获取图片URL，请重试")
                return
            
            # 使用AstrBot的下载功能
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url) as response:
                    if response.status != 200:
                        yield event.plain_result("图片下载失败，请重试")
                        return
                    img_data = await response.read()
                    query_img = Image.open(BytesIO(img_data)).convert('RGB')
            
            # 显示处理进度
            yield event.plain_result(f"🔍 正在识别{'（效果: ' + effect + '）' if effect else ''}...")
            
            # 查找最匹配的卡牌
            match_name, score = self.find_best_match(query_img, effect)
            
            if match_name:
                # 计算相似度百分比（将距离转换为相似度）
                # 距离0为完全匹配，最大距离256
                similarity = max(0, 100 - (score / 256 * 100))
                yield event.plain_result(
                    f"🎯 我猜这张卡牌是：**{match_name}**\n"
                    f"📊 相似度：{similarity:.1f}%\n"
                    f"🔢 匹配分数：{score:.2f}"
                )
            else:
                yield event.plain_result(
                    f"😅 没认出来这张卡牌...\n"
                    f"匹配分数：{score:.2f}（超过阈值）\n"
                    f"💡 提示：请确保卡牌图片清晰，或尝试使用不同的效果"
                )
                
        except Exception as e:
            logger.error(f"猜卡牌出错: {e}")
            yield event.plain_result(f"❌ 处理出错: {str(e)}")

    @filter.command("添加卡牌")
    async def add_card(self, event: AstrMessageEvent):
        '''添加卡牌到数据库：发送卡牌图片并指定卡牌名
        用法：/添加卡牌 卡牌名'''
        
        # 检查权限（只有管理员可以添加）
        if not event.is_admin():
            yield event.plain_result("❌ 只有群管理员可以添加卡牌")
            return
        
        # 解析卡牌名
        if not event.message_str:
            yield event.plain_result("请指定卡牌名！\n用法：/添加卡牌 卡牌名")
            return
        
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("请指定卡牌名！\n用法：/添加卡牌 卡牌名")
            return
        
        card_name = parts[1].strip()
        if not card_name:
            yield event.plain_result("卡牌名不能为空！")
            return
        
        # 检查是否有图片
        if not event.message_obj or not hasattr(event.message_obj, 'message'):
            yield event.plain_result("请同时发送一张卡牌图片！")
            return
        
        image_segments = [seg for seg in event.message_obj.message if isinstance(seg, Comp.Image)]
        if not image_segments:
            yield event.plain_result("未检测到图片，请同时发送一张卡牌图片！")
            return
        
        # 下载图片
        try:
            img_url = image_segments[0].url
            if not img_url:
                yield event.plain_result("无法获取图片URL，请重试")
                return
            
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url) as response:
                    if response.status != 200:
                        yield event.plain_result("图片下载失败，请重试")
                        return
                    img_data = await response.read()
                    img = Image.open(BytesIO(img_data)).convert('RGB')
            
            # 保存图片
            # 清理文件名中的非法字符
            safe_name = "".join(c for c in card_name if c.isalnum() or c in (' ', '-', '_'))
            if not safe_name:
                safe_name = f"card_{int(asyncio.get_event_loop().time())}"
            
            # 确保文件名唯一
            save_path = os.path.join(self.cards_dir, f"{safe_name}.png")
            counter = 1
            while os.path.exists(save_path):
                save_path = os.path.join(self.cards_dir, f"{safe_name}_{counter}.png")
                counter += 1
            
            img.save(save_path, "PNG")
            
            # 重新加载卡牌数据库
            self.load_card_database()
            
            yield event.plain_result(f"✅ 卡牌 '{card_name}' 已成功添加到数据库！\n📁 保存路径：{save_path}")
            
        except Exception as e:
            logger.error(f"添加卡牌出错: {e}")
            yield event.plain_result(f"❌ 添加失败: {str(e)}")

    @filter.command("卡牌列表")
    async def list_cards(self, event: AstrMessageEvent):
        '''列出所有已添加的卡牌'''
        if not self.card_hashes:
            yield event.plain_result("📭 卡牌数据库为空，请先使用 /添加卡牌 添加卡牌")
            return
        
        card_list = sorted(self.card_hashes.keys())
        # 如果太多，分页显示
        if len(card_list) > 20:
            # 简单处理，只显示前20个
            display = card_list[:20]
            result = f"📚 共有 {len(card_list)} 张卡牌（显示前20张）：\n"
            result += "\n".join(f"• {name}" for name in display)
            result += f"\n... 还有 {len(card_list) - 20} 张"
        else:
            result = f"📚 共有 {len(card_list)} 张卡牌：\n"
            result += "\n".join(f"• {name}" for name in card_list)
        
        yield event.plain_result(result)

    async def terminate(self):
        '''插件卸载时调用'''
        logger.info("Kards猜卡牌插件已卸载")
