# Archived v1 script. The maintained entry point is ../Information_xhs.py.
import re

import os

import time

import threading

from datetime import datetime, timedelta

import pandas as pd

import requests



from PIL import Image

from selenium import webdriver

from selenium.webdriver.common.by import By

from selenium.webdriver.common.keys import Keys

from selenium.webdriver.support.wait import WebDriverWait

from selenium.webdriver.support import expected_conditions as EC

from selenium.webdriver.common.action_chains import ActionChains



'''

根据目前的程序架构，parameter.txt 中各参数的功能定义如下：



1. keyword (关键词)：

   - 作用：定义 {BASE_STORAGE_PATH}/{keyword}/ 存储主目录。

   - 影响：当 over=1 时，作为小红书搜索框的输入词。



2. wait_time (关键词搜索停留时间)：

   - 作用：控制搜索结果页的“滚屏”时长（秒）。

   - 场景：仅在 over=1 的搜索阶段有效，决定了采集链接的数量。



3. interval_time (帖子页面停留时间)：

   - 作用：在具体帖子详情页的停留时间（秒）。

   - 注意：目前在代码中固定使用 time.sleep(3)，该参数保留作为扩展。



4. over (运行模式选择)：

   - 0：直接下载模式。仅处理 parameter.txt 中列出的现有链接。

   - 1：搜索+下载双阶段模式。

     - 阶段一：开启浏览器搜索关键词并采集 URL，保存至本地 txt（带分隔符）。

     - 阶段二：关闭搜索窗口，开启新窗口自动进入“直接下载模式”处理所有 URL。



5. select (图片命名方式)：

   - 0：默认命名。

   - 1：强制使用“帖子标题”命名。

   - 2：强制使用“描述内容首行”命名。



6. headless (窗口运行模式)：

   - 0：正常显示浏览器窗口。

   - 1：窗口最小化运行。

     - 注意：若同时设置 login_only=1，则在登录验证完成后才自动最小化。



7. login_only (仅登录模式)：

   - 0：全自动模式。开启后台线程自动监控并关闭所有登录/验证弹窗。

   - 1：手动登录模式。关闭所有监控逻辑，方便用户在搜索阶段手动完成登录和验证。

   - 特别注意：程序进入“下载阶段”后，该参数将被强制设为 0（即下载过程始终开启自动弹窗监控）。



程序流程：

[搜索阶段 (over=1)] -> 保存/追加 URL 到本地文件 -> [下载阶段 (自动开启新窗口，login_only 强制为 0)]

'''





# ==========================================

# 顶层配置参数 (Top Level Configuration)

# ==========================================



# 配置自启动的浏览器参数

# 注意：全局只声明，不初始化

driver = None

wait = None



# 增加全局变量控制线程

stop_monitor = False

login_only_mode = 0  # 0表示开启监控，1表示仅登录关闭监控



# 图片及报告的存储根目录

BASE_STORAGE_PATH = './Information'

# 参数配置文件路径

PARAMETER_FILE = './parameter.txt'

# ==========================================



def clean_filename(filename):

    """

    清洗文件名，移除 Windows/Linux 不允许的特殊字符，替换为空字符串

    """

    # Windows 不允许的字符: \ / : * ? " < > |

    invalid_chars = r'[\\/:*?"<>|]'

    return re.sub(invalid_chars, '', filename).strip()





def parse_date(date_str):

    now = datetime.now()

    try:

        if '天前' in date_str:

            days = int(re.search(r'(\d+)天前', date_str).group(1))

            return (now - timedelta(days=days)).strftime('%Y-%m-%d')

        elif '小时前' in date_str:

            hours = int(re.search(r'(\d+)小时前', date_str).group(1))

            return (now - timedelta(hours=hours)).strftime('%Y-%m-%d')

        elif '分钟前' in date_str:

            minutes = int(re.search(r'(\d+)分钟前', date_str).group(1))

            return (now - timedelta(minutes=minutes)).strftime('%Y-%m-%d')

        elif '昨天' in date_str:

            return (now - timedelta(days=1)).strftime('%Y-%m-%d')

        else:

            # 尝试直接匹配 2024-04-28 这种格式

            match = re.search(r'(\d{4}-\d{2}-\d{2})', date_str)

            if match:

                return match.group(1)

            # 处理 04-28 这种格式（补全当前年份）

            match = re.search(r'(\d{2}-\d{2})', date_str)

            if match:

                return f"{now.year}-{match.group(1)}"

    except:

        pass

    return date_str





# 自动关闭登录弹窗的函数

def monitor_login_window(timeout=60):

    global stop_monitor

    print("后台监控线程已开启...")

    start_time = time.time()

    while not stop_monitor and (time.time() - start_time < timeout):

        try:

            # 使用用户提供的最新选择器和 XPath

            close_btn = driver.find_elements(By.CSS_SELECTOR, 'div.login-container div.icon-btn-wrapper.button.close, div.login-container div.icon-btn-wrapper.close-button')

            if not close_btn:

                close_btn = driver.find_elements(By.XPATH, '/html/body/div[2]/div[1]/div/div[1]/div[1]')

            

            if close_btn and close_btn[0].is_displayed():

                close_btn[0].click()

                print("已自动从后台线程关闭登录弹窗")

        except:

            pass

        time.sleep(0.5) # 每0.5秒检查一次，响应更快

    print("后台监控线程已关闭")





def close_login_window():

    # 之前的手动监控逻辑保留作为同步调用

    try:

        close_btn = driver.find_elements(By.CSS_SELECTOR, 'div.login-container div.icon-btn-wrapper.button.close, div.login-container div.icon-btn-wrapper.close-button')

        if not close_btn:

            close_btn = driver.find_elements(By.XPATH, '/html/body/div[2]/div[1]/div/div[1]/div[1]')

        if close_btn and close_btn[0].is_displayed():

            close_btn[0].click()

            print("已通过同步调用关闭登录弹窗")

            return True

    except:

        pass

    return False





# 爬取 img

def spider(url, select=None, headless_enabled=0):

    # 增加返回值用于统计成功/失败

    try:

        # 针对 Windows 系统，防止新标签页弹出并夺取焦点的终极逻辑

        if headless_enabled == 1:

            try:

                # 1. 切换回主窗口并最小化，确保状态统一

                driver.switch_to.window(driver.window_handles[0])

                driver.minimize_window()

            except:

                pass



        # 使用 JS 开启新窗口

        driver.execute_script("window.open('about:blank', '_blank');")

        

        # 立即获取句柄并切换

        window_handles = driver.window_handles

        driver.switch_to.window(window_handles[-1])

        

        # 如果是最小化模式，执行饱和式压制

        if headless_enabled == 1:

            # 在跳转 URL 之前，执行多次压制操作

            for _ in range(10):

                try:

                    # 关键修复：恢复正常尺寸 1024x768，但保持在屏幕外

                    # 尺寸过小（如1x1）会导致小红书加载移动端甚至异常布局，从而无法提取元数据

                    driver.set_window_rect(x=-4000, y=-4000, width=1024, height=768)

                    driver.minimize_window()

                except:

                    pass

        

        driver.get(url)



        # 增加延时确保页面在后台加载完毕

        time.sleep(3)



        # 1. 自动关闭登录弹窗

        close_login_window()



        # 提取元数据

        # 使用 find_elements 防止单个元素找不到直接报错跳入 except

        try:

            title_elems = driver.find_elements(By.CSS_SELECTOR, '#detail-title')

            post_title = title_elems[0].text if title_elems else "无标题"

        except:

            post_title = "无标题"



        try:

            desc_elems = driver.find_elements(By.CSS_SELECTOR, '#detail-desc')

            if not desc_elems:

                # 使用用户提供的 XPath 兜底

                desc_elems = driver.find_elements(By.XPATH, '/html/body/div[2]/div[1]/div[2]/div[2]/div/div[1]/div[4]/div[2]/div[1]/div[2]')

            

            if desc_elems:

                # 提取所有文本信息

                raw_content = desc_elems[0].text

                # 优化排版：去除连续多余的空行，只保留必要的单空行

                post_content = re.sub(r'\n\s*\n', '\n\n', raw_content).strip()

            else:

                post_content = ""

        except:

            post_content = ""



        try:

            # 优先使用用户提供的精准选择器

            author_selectors = [

                (By.CSS_SELECTOR, '#noteContainer > div.interaction-container > div.author-container > div > div.info > a > span'),

                (By.XPATH, '/html/body/div[2]/div[1]/div[2]/div[2]/div/div[1]/div[4]/div[1]/div/div[1]/a/span'),

                (By.CLASS_NAME, 'username'),

                (By.CSS_SELECTOR, '.username'),

                (By.CSS_SELECTOR, 'div.author-container span.name'),

                (By.CSS_SELECTOR, '.author-name')

            ]

            

            author_elems = []

            for method, value in author_selectors:

                author_elems = driver.find_elements(method, value)

                if author_elems and author_elems[0].text.strip():

                    print(f"成功获取作者名，使用方法: {method} 内容: {value}")

                    break

                

            author = author_elems[0].text.strip() if author_elems else "未知作者"

        except:

            author = "未知作者"



        try:

            date_location_elems = driver.find_elements(By.CSS_SELECTOR, '#noteContainer > div.interaction-container > div.note-scroller > div.note-content > div.bottom-container > span.date')

            if date_location_elems:

                raw_text = date_location_elems[0].text # 例如 "编辑于 昨天 11:20 上海" 或 "2023-02-15 上海"

                clean_text = raw_text.replace('编辑于', '').strip()

                parts = clean_text.split(' ')

                parts = [p for p in parts if p.strip()]

                if parts:

                    raw_date = parts[0]

                    location = parts[-1] if len(parts) > 1 else "未知地点"

                    publish_date = parse_date(raw_date)

                else:

                    publish_date = datetime.now().strftime('%Y-%m-%d')

                    location = "未知地点"

            else:

                publish_date = datetime.now().strftime('%Y-%m-%d')

                location = "未知地点"

        except:

            publish_date = datetime.now().strftime('%Y-%m-%d')

            location = "未知地点"



        # 类型判断初始值

        post_type = "文字"



        # 处理旧逻辑中的 Title 变量用于文件名

        try:

            # 获取原始标题，如果为“无标题”则使用关键词

            raw_title = post_title if post_title != "无标题" else keyword

            # 清洗文件名，移除非法字符

            base_title = clean_filename(raw_title)

            

            # 根据用户选择（select参数）决定最终基础名

            if select == 1:

                Title = clean_filename(post_title)

            elif select == 2:

                Title = clean_filename(post_content.split('\n')[0]) if post_content else clean_filename(keyword)

            else:

                Title = base_title

                

            # 确保 Title 不为空

            if not Title: Title = "unnamed_post"

        except:

            Title = clean_filename(keyword)

        



        try:

            # 爬取图片/视频封面

            # 1. 检查是否是视频帖子

            video_poster = driver.find_elements(By.CSS_SELECTOR, 'xg-poster')

            if video_poster:

                post_type = "视频"

                poster_style = video_poster[0].get_attribute('style')

                match = re.search(r'url\("(.*?)"\)', poster_style)

                if match:

                    image_url = match.group(1)

                    response = requests.get(image_url)

                    # 图片存储在 Images 子目录下

                    img_dir = os.path.join(BASE_STORAGE_PATH, keyword, "Images")

                    if not os.path.exists(img_dir):

                        os.makedirs(img_dir)

                    

                    filename = f"{Title}_封面.png"

                    path = os.path.join(img_dir, filename)

                    with open(file=path, mode='wb') as f:

                        f.write(response.content)

                    Image.open(path).save(path)

                    print("视频封面已下载")

            else:

                # 2. 图片爬取逻辑

                try:

                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div.swiper-slide img')))

                except:

                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, 'swiper-slide')))



                images_elements = driver.find_elements(By.CSS_SELECTOR, 'div.note-slider img, div.live-video-wrapper img, .swiper-slide img, .swiper-slide')

                

                if images_elements:

                    post_type = "图片"

                

                downloaded_urls = []

                downloaded_paths = []

                # 过滤掉最后一张图片 (如果是多图)

                if len(images_elements) > 1:

                    process_elements = images_elements[:-1]

                else:

                    process_elements = images_elements



                # 特殊命名逻辑：第一张成功下载的图先命名为 0

                first_img_path = None

                success_download_count = 0

                

                for element in process_elements:

                    image_url = None

                    if element.tag_name == 'img':

                        image_url = element.get_attribute('src')

                    else:

                        image_style = element.get_attribute('style')

                        if image_style:

                            match = re.search(r'url\("(.*?)"\)', image_style)

                            if match:

                                image_url = match.group(1)

                    

                    if not image_url or 'http' not in image_url or image_url in downloaded_urls:

                        continue

                        

                    try:

                        response = requests.get(image_url)

                        if response.status_code != 200:

                            continue

                            

                        downloaded_urls.append(image_url)

                        

                        # 只有成功下载才增加计数

                        # 第一张成功下载的图后缀记为 0

                        current_suffix = 0 if success_download_count == 0 else success_download_count

                        filename = f'{Title}{current_suffix}.png'

                        

                        # 图片存储在 Images 子目录下

                        img_dir = os.path.join(BASE_STORAGE_PATH, keyword, "Images")

                        if not os.path.exists(img_dir):

                            os.makedirs(img_dir)

                            

                        path = os.path.join(img_dir, filename)

                        with open(file=path, mode='wb') as f:

                            f.write(response.content)

                        Image.open(path).save(path)

                        

                        if success_download_count == 0:

                            first_img_path = path

                        else:

                            downloaded_paths.append(filename)

                            

                        success_download_count += 1

                        

                    except Exception as e:

                        print(f"下载图片失败: {e}")

                        continue



                # 处理完成后，将后缀为 0 的第一张图重命名为 N (当前 success_download_count)

                if first_img_path and os.path.exists(first_img_path):

                    # 如果只有一张图，序号就是1；如果有N张图，第一张(0)重命名为N

                    new_first_filename = f'{Title}{success_download_count}.png'

                    new_first_path = os.path.join(os.path.dirname(first_img_path), new_first_filename)

                    os.rename(first_img_path, new_first_path)

                    # 将第一张图（重命名后）放回列表末尾

                    downloaded_paths.append(new_first_filename)



            print(url, "已完成下载！！！")



            # 写入 Markdown 文件

            md_path = os.path.join(BASE_STORAGE_PATH, keyword, f'{keyword}.md')

            md_text_only_path = os.path.join(BASE_STORAGE_PATH, keyword, f'{keyword}【纯文字版】.md')

            

            # 1. 写入标准版 (带图片)

            with open(md_path, 'a', encoding='utf-8') as md_file:

                md_file.write(f'# {post_title}\n\n')

                md_file.write(f'- **作者**: {author}\n')

                md_file.write(f'- **类型**: {post_type}\n')

                md_file.write(f'- **发布时间**: {publish_date}\n')

                md_file.write(f'- **发布地点**: {location}\n')

                md_file.write(f'- **链接**: [{url}]({url})\n')

                # 记录本次抓取的图片数量和基础文件名

                img_info = f"共 {len(downloaded_paths)} 张" if post_type == "图片" else "视频封面"

                md_file.write(f'- **本地图片目录**: `{keyword}/Images/` ({img_info},基础名: `{Title}`)\n\n')

                

                # 新增：将图片嵌入 Markdown

                md_file.write('### 图片展示\n\n')

                if post_type == "视频":

                    poster_name = f"{Title}_封面.png"

                    # 使用相对路径，指向 Images 文件夹

                    md_file.write(f'![{Title}_封面](Images/{poster_name})\n\n')

                else:

                    for img_name in downloaded_paths:

                        # 使用相对路径，指向 Images 文件夹

                        md_file.write(f'![{img_name}](Images/{img_name})\n\n')



                md_file.write('## 内容\n\n')

                md_file.write(f'{post_content}\n\n')

                md_file.write('---\n\n')



            # 2. 写入纯文字版

            with open(md_text_only_path, 'a', encoding='utf-8') as md_file:

                md_file.write(f'# {post_title}\n\n')

                md_file.write(f'- **作者**: {author}\n')

                md_file.write(f'- **类型**: {post_type}\n')

                md_file.write(f'- **发布时间**: {publish_date}\n')

                md_file.write(f'- **发布地点**: {location}\n')

                md_file.write(f'- **链接**: [{url}]({url})\n')

                # 记录本次抓取的图片数量和基础文件名

                img_info = f"共 {len(downloaded_paths)} 张" if post_type == "图片" else "视频封面"

                md_file.write(f'- **本地图片目录**: `{keyword}/Images/` ({img_info},基础名: `{Title}`)\n\n')

                

                md_file.write('## 内容\n\n')

                md_file.write(f'{post_content}\n\n')

                md_file.write('---\n\n')



            # 爬取完成后关闭详情页监控

            stop_monitor = True

            # 关闭新窗口

            driver.close()

            # 切回到原始窗口

            driver.switch_to.window(window_handles[0])

            return True, None



        except Exception as e:

            print(f"提取出错: {e}")

            print('帖子标题为:“{0}”无图片提取!!!  url:{1}'.format(Title,url))

            # 关闭新窗口

            driver.close()

            # 切回到原始窗口

            driver.switch_to.window(window_handles[0])

            return False, url



    except Exception as e:

        print(f"打开页面出错: {e}")

        return False, url





# 爬取 url

def get_url(url,Key_words,over):

    global stop_monitor

    driver.get(url)

    

    if login_only_mode == 0:

        # 开启后台持续监控线程

        stop_monitor = False

        monitor_thread = threading.Thread(target=monitor_login_window, args=(120,), daemon=True)

        monitor_thread.start()

    else:

        print("当前处于‘仅登录模式’，已禁用自动弹窗监视，请手动完成验证。")



    if over == 1:

        input('请在浏览器中登录小红书并完成验证，然后回到这里按回车键继续...')

        

        # 尝试多次定位搜索框，小红书在不同状态下 HTML 结构可能不同

        search = None

        selectors = [

            (By.ID, 'search-input'),

            (By.XPATH, '//input[@id="search-input"]'),

            (By.CSS_SELECTOR, 'input.search-input'),

            (By.XPATH, '/html/body/div[1]/div[1]/div[1]/header/div[2]/input'),

            (By.XPATH, '/html/body/div[2]/div[1]/div[1]/header/div[1]/input[2]'),

            (By.CSS_SELECTOR, 'header input')

        ]

        

        for method, value in selectors:

            try:

                search = driver.find_element(method, value)

                if search.is_displayed():

                    print(f"成功通过 {method} 找到搜索框")

                    break

            except:

                continue

        

        if search is None:

            print("警告：未能自动找到搜索框，请手动在浏览器中点击搜索框后再试。")

            input("请手动点击搜索框，然后回到这里按回车...")

            search = driver.switch_to.active_element



        # 全选文本框中的内容

        search.send_keys(Keys.CONTROL, 'a')

        # 删除选定的内容

        search.send_keys(Keys.BACKSPACE)



        # 输入关键词前先停顿，确保页面稳定

        time.sleep(1)

        search.send_keys(Key_words)

        time.sleep(1)

        

        # 输入后可能立即弹出登录窗，再次触发监控

        close_login_window()

        

        # 模拟按下回车键并停留片刻确保搜索响应

        search.send_keys(Keys.ENTER)

        

        # 回车后可能立即弹出登录窗，再次触发监控

        close_login_window()

        

        print(f"已输入关键词 '{Key_words}' 并执行回车搜索，准备采集新链接...")

        # 重要：回车搜索后给予页面充足的跳转和加载时间，防止采集到搜索前页面的旧链接

        time.sleep(5) 



        # 核心修改：在确认已经进入搜索结果页后，才开始初始化采集列表

        all_urls = []

        

        # 拖动鼠标向下移动

        action = ActionChains(driver)

        # 执行连续的向下滑动操作

        start_time = time.time()

        elapsed_time = 0



        while elapsed_time < wait_time:

            try:

                all_first_urls = driver.find_elements(By.CSS_SELECTOR, 'a.cover.ld.mask')

                for k in all_first_urls:

                    if k.get_attribute('href') not in all_urls:

                        all_urls.append(k.get_attribute('href'))

            except:

                pass

            try:

                all_second_urls = driver.find_elements(By.CSS_SELECTOR, 'a.cover.mask')

                for k in all_second_urls:

                    if k.get_attribute('href') not in all_urls:

                        all_urls.append(k.get_attribute('href'))

            except:

                pass



            print(len(all_urls),end="->")



            action.send_keys(Keys.ARROW_DOWN)

            action.perform()

            elapsed_time = time.time() - start_time



        # first_url = 'https://www.xiaohongshu.com'

        # 搜索完成后关闭后台监控，准备进入详情页

        stop_monitor = True

        time.sleep(1)

        return all_urls



    else:

        return None





if __name__ == '__main__':



    # 设置参数调用函数启动程序

    url = 'https://www.xiaohongshu.com'



    # keyword表示 填写你要爬取的关键词

    # wait_time表示 在关键词页面停留时间

    # interval_time表示 在具体帖子页面评论区停留时间

    # over表示 是否进行登陆式关键词搜素 在0时为不登录 1为登录

    # select表示 选择以何种方式命名png文件，0表示不做要求、1表示按照按照帖子标题、2表示按照帖子关键词



    # keyword = '88碳账户';wait_time = 60;interval_time = 4;over = 1;select = 0



    # 参数从参数配置.txt文件中提取获得:

    with open(PARAMETER_FILE, 'r',encoding='utf-8-sig') as f:

        content = f.readlines()



    # params = content[-1].strip().split('|')

    # 为了兼容旧的 parameter.txt，支持更多可选参数

    params = content[-1].strip().split('|')

    keyword = params[0]

    wait_time = int(params[1])

    interval_time = int(params[2])

    over = int(params[3])

    select = int(params[4])

    headless = int(params[5]) if len(params) > 5 else 0

    login_only_mode = int(params[6]) if len(params) > 6 else 0



    # 配置自启动的浏览器参数

    options = webdriver.ChromeOptions()

    # 针对 Windows 系统，防止新标签页弹出并夺取焦点的最终配置

    if headless == 1:

        options.add_argument('--window-position=-4000,-4000') # 初始位置设在屏幕外

        options.add_argument('--window-size=1,1')            # 初始尺寸设为最小

        # options.add_argument('--headless') # 注意：不使用真无头，因为可能影响弹窗监控逻辑

    

    options.add_argument('--no-sandbox')

    options.add_argument('--disable-dev-shm-usage')

    options.add_argument('--disable-blink-features=AutomationControlled')

    

    # 文件夹准备

    file1 = os.path.join(BASE_STORAGE_PATH, keyword)



    if not os.path.exists(file1):

        os.makedirs(file1)



    # 创建图片存储文件夹

    img_dir = os.path.join(file1, "Images")

    if not os.path.exists(img_dir):

        os.makedirs(img_dir)



    with open(os.path.join(file1, f'{keyword}.txt'), 'w',encoding='utf-8-sig') as fs:

        for i in content:

            fs.write(i)



    print(f"--- 启动爬虫 ---")

    print(f"关键词: {keyword}")

    print(f"待处理链接数量: {len(content[:-1])}")

    

    # 第一阶段：如果 over=1，进行搜索并保存结果

    if over == 1:

        # 在这里初始化搜索用的浏览器

        driver = webdriver.Chrome(options=options)
        
        # 核心逻辑修正：如果开启了 headless 但同时也开启了 login_only 模式
        # 此时需要强制显示窗口，供用户扫码登录
        if headless == 1 and login_only_mode == 1:
            driver.maximize_window()
            print("检测到登录模式，已为您显示窗口，请完成扫码登录。")
        elif headless == 1:
            # 仅在非登录模式下才启动即最小化
            driver.minimize_window()
            print("已开启最小化执行模式...")
            
        wait = WebDriverWait(driver, 10)
            

        new_urls = get_url(url, keyword, over)

        

        # 搜索/登录完成后，如果是 headless=1，则执行最小化

        if headless == 1 and login_only_mode == 1:

            driver.minimize_window()

            print("登录已完成，已切换至最小化执行模式。")

            

        if new_urls:

            # 去重并准备保存

            existing_urls = [l.strip() for l in content[:-1]]

            unique_new_urls = [url for url in new_urls if url.strip() not in existing_urls]

            

            # 更新本地 txt 文件逻辑：原链接 + 分隔符 + 新链接 + 参数行

            txt_path = os.path.join(BASE_STORAGE_PATH, keyword, f'{keyword}.txt')

            with open(txt_path, 'w', encoding='utf-8-sig') as fs:

                # 1. 写入原有的 URL

                for l in content[:-1]:

                    fs.write(l)

                # 2. 写入分隔符

                fs.write("-----------------------------------------------------\n")

                # 3. 写入新增的 URL

                for url in unique_new_urls:

                    fs.write(url + "\n")

                # 4. 写入参数行

                fs.write(content[-1])

            

            print(f"搜索完成，新增 {len(unique_new_urls)} 个链接已保存。总计待处理: {len(existing_urls) + len(unique_new_urls)}")

            

        # 搜索阶段结束，关闭浏览器

        driver.quit()

        driver = None

        print("搜索阶段结束，正在开启新窗口进入下载阶段...")



    # 第二阶段：下载/处理阶段

    # 无论 over 是 0 还是 1，最终都从关键词对应的 txt 文件读取待处理链接

    # (over=0 时，__main__ 部分已经根据 parameter.txt 的最后一行创建了文件夹并初始化了 {keyword}.txt)

    txt_path = os.path.join(BASE_STORAGE_PATH, keyword, f'{keyword}.txt')

    

    # 如果是直接下载模式 (over=0) 且本地 txt 不存在，则需要从 parameter.txt 初始化

    if not os.path.exists(txt_path):

        print(f"检测到本地记录文件不存在，正在从 {PARAMETER_FILE} 初始化...")

        # 已经在 __main__ 中处理了文件夹创建和初始化逻辑

    

    with open(txt_path, 'r', encoding='utf-8-sig') as f:

        updated_content = f.readlines()

    

    # 提取所有有效的 URL（跳过分隔符和最后的参数行）

    all_to_process = []

    for line in updated_content[:-1]:

        line = line.strip()

        if line and "---" not in line:

            all_to_process.append(line)



    print(f"--- 启动下载阶段 ---")

    print(f"待处理链接总数: {len(all_to_process)}")

    

    # 初始化统计

    total_count = len(all_to_process)

    success_count = 0

    failed_urls = []

    start_time_all = time.time()

    

    # 下载阶段固定配置：login_only=0 (不登录, 开启监视), options 保持 headless 设置

    login_only_mode = 0 

    

    for k in all_to_process:

        print(f"正在处理链接: {k}")

        if driver is None:

            # 开启新窗口

            driver = webdriver.Chrome(options=options)

            # 根据 headless 设置决定是否最小化

            if headless == 1:

                driver.minimize_window()

                print("下载阶段：已开启最小化执行模式...")

            wait = WebDriverWait(driver, 10)

        

        success, failed_url = spider(k, select, headless_enabled=headless)

    

        if success:

            success_count += 1

        else:

            failed_urls.append(failed_url)

    

    end_time_all = time.time()

    duration = end_time_all - start_time_all

    

    # 写入 Log.txt (追加模式)

    log_path = os.path.join(BASE_STORAGE_PATH, keyword, 'Log.txt')

    with open(log_path, 'a', encoding='utf-8') as log_file:

        log_file.write(f"\n处理汇总 (记录时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())})\n")

        log_file.write(f"==========================================\n")

        log_file.write(f"关键词: {keyword}\n")

        log_file.write(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time_all))}\n")

        log_file.write(f"结束时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time_all))}\n")

        log_file.write(f"总耗时: {duration:.2f} 秒\n")

        log_file.write(f"处理 URL 总数: {total_count}\n")

        log_file.write(f"成功实现数量: {success_count}\n")

        log_file.write(f"失败数量: {len(failed_urls)}\n")

        if failed_urls:

            log_file.write(f"失败 URL 详情:\n")

            for furl in failed_urls:

                log_file.write(f"- {furl}\n")

        log_file.write(f"==========================================\n")

    

    print(f"日志已保存至: {log_path}")

    

    if driver:

        driver.quit()

    print("--- 所有任务已完成 ---")
