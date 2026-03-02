# =============================================================================
# TeslaMate Drive Visualizer
# 
# Renders TeslaMate driving data into an overlay video with real-time stats.
# Supports green screen (chroma key) compositing.
#
# MIT License
# Copyright (c) 2025
# https://github.com/YOUR_USERNAME/teslamate-drive-visualizer
# =============================================================================

import warnings
# 抑制所有警告
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=ImportWarning)
warnings.filterwarnings('ignore', module='pandas')
warnings.filterwarnings('ignore', module='psycopg2')
warnings.filterwarnings('ignore', module='matplotlib')

import psycopg2
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib import patheffects
import os
import platform
from tqdm import tqdm
from datetime import timedelta
import matplotlib.font_manager as fm
from scipy import interpolate

# 检查并设置可用的字体 - 自动适配 Mac / Windows / Linux
def setup_font():
    system = platform.system()

    if system == 'Darwin':  # macOS
        preferred_fonts = [
            'PingFang SC', 'Hiragino Sans GB', 'STHeiti', 'Heiti SC',
            'Apple LiGothic Medium', 'Apple LiSung Light', 'STFangsong'
        ]
    elif system == 'Windows':
        preferred_fonts = [
            'Microsoft YaHei', 'Microsoft YaHei UI', 'SimHei',
            'SimSun', 'NSimSun', 'KaiTi', 'FangSong'
        ]
    else:  # Linux
        preferred_fonts = [
            'Noto Sans CJK SC', 'Noto Sans SC', 'WenQuanYi Micro Hei',
            'WenQuanYi Zen Hei', 'Droid Sans Fallback'
        ]

    all_fonts = [f.name for f in fm.fontManager.ttflist]

    available_font = None
    for font in preferred_fonts:
        if any(font in f for f in all_fonts):
            available_font = font
            break

    if available_font:
        plt.rcParams['font.family'] = available_font
        print(f"✅ 使用字体: {available_font} ({system})")
    else:
        plt.rcParams['font.family'] = ['sans-serif']
        print(f"⚠️  未找到适合 {system} 的中文字体，中文可能显示为方块")
        if system == 'Linux':
            print("   Linux 用户请安装：sudo apt install fonts-noto-cjk")
        elif system == 'Windows':
            print("   Windows 用户请确认系统已安装中文语言包")

    plt.rcParams['axes.unicode_minus'] = False

# 初始化字体设置
setup_font()

# ==============================
# ⚙️ 数据库配置
# 
# 【本地连接】TeslaMate 和本脚本运行在同一台机器上：
#   DB_HOST = "localhost"
#
# 【网络连接】TeslaMate 运行在局域网其他设备（如 NAS、树莓派）：
#   DB_HOST = "192.168.1.100"   ← 填写那台设备的 IP
#
# 密码在 TeslaMate 的 docker-compose.yml 中 POSTGRES_PASSWORD= 后面
# 端口需与 docker-compose.yml 中 ports 左边的值一致（详见 README）
# ==============================
DB_HOST = "YOUR_TESLAMATE_IP"       # 本地填 "localhost"，远程填设备 IP
DB_PORT = 55432                      # 与 docker-compose.yml ports 左边端口一致（可自定义）
DB_NAME = "teslamate"                # 数据库名称（默认不需要修改）
DB_USER = "teslamate"                # 数据库用户名（默认不需要修改）
DB_PASSWORD = "YOUR_DB_PASSWORD"     # 数据库密码（docker-compose.yml 中 POSTGRES_PASSWORD=）

# ==============================
# 🎬 视频参数
# ==============================
OUTPUT_DIR = "./"                    # 视频输出目录，默认为当前目录
FPS = 30                             # 帧率
PAUSE_GAP_MIN = 10                   # 小于此间隔(分钟)的行程自动合并为一段

# ==============================
# 💰 费用参数
# ==============================
ELECTRICITY_PRICE = 0.5              # 电费单价，元/度(kWh)，请根据当地电价修改

# ==============================
# 🔋 能效参数
# 标准能耗用于计算续航达成率
# 155 Wh/km 对应 Model 3 Performance 1:1 达成率
# 请根据你的车型调整此值
# ==============================
STANDARD_EFFICIENCY = 155            # Wh/km

# ==============================
# 🌏 时区设置
# 常用时区：Asia/Shanghai / Asia/Taipei / Asia/Tokyo / Europe/London / America/New_York
# ==============================
LOCAL_TIMEZONE = 'Asia/Shanghai'     # 请修改为你所在的时区

# ==============================
# 📈 插值参数
# ==============================
INTERPOLATION_FACTOR = 4             # 数据插值倍数，数值越高视频越平滑，但生成越慢

# ==============================
# 🧭 辅助函数
# ==============================

def connect_db():
    """连接数据库，隐藏所有警告"""
    # 临时设置环境变量抑制PostgreSQL警告
    os.environ['PGOPTIONS'] = '-c client_min_messages=ERROR'
    
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )
    # 设置连接级别不显示警告
    conn.set_session(autocommit=True)
    return conn

def get_cars():
    """获取车辆列表"""
    sql = """
        SELECT 
            id,
            name,
            vin,
            efficiency
        FROM cars
        ORDER BY id;
    """
    try:
        with connect_db() as conn:
            df = pd.read_sql(sql, conn)
        return df
    except Exception as e:
        print(f"❌ 获取车辆列表失败: {e}")
        return pd.DataFrame()

def get_recent_drives(car_id, limit=20):
    """获取指定车辆的最近行程"""
    sql = """
        SELECT 
            id, 
            start_date, 
            end_date,
            distance, 
            duration_min,
            start_km,
            end_km,
            car_id
        FROM drives
        WHERE car_id = %s
        ORDER BY start_date DESC
        LIMIT %s;
    """
    with connect_db() as conn:
        df = pd.read_sql(sql, conn, params=(int(car_id), limit))
    
    # 转换时区 - 兼容有无时区信息的时间戳
    for col in ["start_date", "end_date"]:
        ts = pd.to_datetime(df[col])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize('UTC')
        df[col] = ts.dt.tz_convert(LOCAL_TIMEZONE)
    
    return df.sort_values("start_date")

def get_address_from_id(address_id):
    """根据地址ID获取地址名称"""
    if pd.isna(address_id):
        return "未知位置/Unknown"
    
    sql = "SELECT name FROM addresses WHERE id = %s;"
    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(address_id),))
                result = cur.fetchone()
                return result[0] if result else "未知位置/Unknown"
    except:
        return "未知位置/Unknown"

def get_drives_with_addresses(car_id, limit=20):
    """获取包含地址信息的行程"""
    drives = get_recent_drives(car_id, limit)
    
    if len(drives) == 0:
        return drives
    
    # 获取地址信息
    drive_ids = tuple(drives['id'].tolist())
    
    try:
        sql = """
            SELECT 
                d.id,
                d.start_address_id,
                d.end_address_id
            FROM drives d
            WHERE d.id IN %s;
        """
        with connect_db() as conn:
            address_df = pd.read_sql(sql, conn, params=(drive_ids,))
        
        # 合并地址信息
        drives = drives.merge(address_df, on='id', how='left')
        
        # 获取地址名称
        print("🗺️  获取地址信息中...")
        drives['start_address'] = drives['start_address_id'].apply(lambda x: get_address_from_id(x) if pd.notna(x) else "未知起点/Unknown Start")
        drives['end_address'] = drives['end_address_id'].apply(lambda x: get_address_from_id(x) if pd.notna(x) else "未知终点/Unknown End")
        
    except Exception as e:
        print(f"⚠️  获取地址信息失败: {e}")
        drives['start_address'] = "未知起点/Unknown Start"
        drives['end_address'] = "未知终点/Unknown End"
    
    return drives

def merge_continuous_drives(drives):
    """自动合并中断时间短的行程"""
    if len(drives) == 0:
        return []
    
    merged = []
    current = [drives.iloc[0]]
    
    for i in range(1, len(drives)):
        current_end = current[-1]["end_date"] if pd.notna(current[-1]["end_date"]) else current[-1]["start_date"]
        next_start = drives.iloc[i]["start_date"]
        
        gap = (next_start - current_end).total_seconds() / 60
        
        if gap < PAUSE_GAP_MIN:
            current.append(drives.iloc[i])
        else:
            merged.append(current)
            current = [drives.iloc[i]]
    
    merged.append(current)
    return merged

def get_drive_energy_stats(drive_id):
    """获取行程的能耗统计数据 - 使用Grafana中的计算方法"""
    try:
        # 确保drive_id是Python原生int类型
        drive_id_int = int(drive_id)
        
        # 获取净能耗 - 使用Grafana中的计算方法
        net_energy_sql = """
            SELECT
                (NULLIF(GREATEST(start_rated_range_km - end_rated_range_km, 0), 0) * car.efficiency) as net_energy
            FROM drives d
            JOIN cars car ON car.id = car_id
            WHERE d.id = %s;
        """
        
        # 获取能量回收 - 使用Grafana中的计算方法
        regen_energy_sql = """
            with data as (
                select
                    p.power,
                    extract (second from p.date - lag(p.date) over (order by p.date)) as seconds
                from positions p
                where drive_id = %s and power < 0
            )
            select sum(power * (seconds / 3600)) * -1 from data where seconds is not null and seconds < 1.5;
        """
        
        with connect_db() as conn:
            with conn.cursor() as cur:
                # 获取净能耗
                cur.execute(net_energy_sql, (drive_id_int,))
                net_energy_result = cur.fetchone()
                net_energy = float(net_energy_result[0]) if net_energy_result and net_energy_result[0] is not None else 0
                
                # 获取能量回收
                cur.execute(regen_energy_sql, (drive_id_int,))
                regen_energy_result = cur.fetchone()
                regen_energy = float(regen_energy_result[0]) if regen_energy_result and regen_energy_result[0] is not None else 0
                
                return {
                    "net_energy": net_energy,
                    "regen_energy": regen_energy
                }
    except Exception as e:
        print(f"⚠️  获取能耗数据失败: {e}")
        return {"net_energy": 0, "regen_energy": 0}

def get_merged_drive_info(merged_drives):
    """获取合并行程的统计信息"""
    if not merged_drives:
        return None
    
    total_distance = sum(float(drive["distance"]) for drive in merged_drives if pd.notna(drive["distance"]))
    total_duration = sum(float(drive["duration_min"]) for drive in merged_drives if pd.notna(drive["duration_min"]))
    
    # 计算合并行程的总能耗
    total_net_energy = 0
    total_regen_energy = 0
    
    print("🔋 计算行程能耗数据中...")
    for drive in merged_drives:
        energy_stats = get_drive_energy_stats(drive["id"])
        total_net_energy += energy_stats["net_energy"]
        total_regen_energy += energy_stats["regen_energy"]
    
    start_drive = merged_drives[0]
    end_drive = merged_drives[-1]
    
    return {
        "drive_ids": [int(drive["id"]) for drive in merged_drives],
        "start_date": start_drive["start_date"],
        "end_date": end_drive["end_date"],
        "total_distance": total_distance,
        "total_duration": total_duration,
        "total_net_energy": total_net_energy,
        "total_regen_energy": total_regen_energy,
        "start_address": start_drive.get("start_address", "未知起点/Unknown Start"),
        "end_address": end_drive.get("end_address", "未知终点/Unknown End"),
    }

def interpolate_data(df):
    """数据插值以提高时间分辨率 - 修复重复时间戳问题"""
    print(f"📊 数据插值中 (倍数: {INTERPOLATION_FACTOR}x)...")
    
    # 确保数据按时间排序
    df = df.sort_values('date').reset_index(drop=True)
    
    # 检查并处理重复的时间戳
    duplicate_mask = df.duplicated('date', keep='first')
    if duplicate_mask.any():
        print(f"⚠️  发现 {duplicate_mask.sum()} 个重复时间戳，正在处理...")
        df = df[~duplicate_mask].reset_index(drop=True)
    
    # 计算时间戳（秒）
    df['timestamp'] = (df['date'] - df['date'].iloc[0]).dt.total_seconds()
    
    # 再次检查时间戳是否严格递增
    timestamp_diffs = np.diff(df['timestamp'])
    if np.any(timestamp_diffs <= 0):
        print("⚠️  时间戳不严格递增，进行额外处理...")
        # 确保时间戳严格递增
        for i in range(1, len(df)):
            if df['timestamp'].iloc[i] <= df['timestamp'].iloc[i-1]:
                df.loc[df.index[i], 'timestamp'] = df['timestamp'].iloc[i-1] + 0.1
    
    # 创建新的时间序列（更高的分辨率）
    original_timestamps = df['timestamp'].values
    new_timestamps = np.linspace(
        original_timestamps[0], 
        original_timestamps[-1], 
        len(original_timestamps) * INTERPOLATION_FACTOR
    )
    
    # 创建新的DataFrame
    new_df = pd.DataFrame()
    new_df['timestamp'] = new_timestamps
    new_df['date'] = df['date'].iloc[0] + pd.to_timedelta(new_timestamps, unit='s')
    
    # 对数值列进行插值 - 优先使用线性插值避免重复时间戳问题
    numeric_columns = ['speed', 'power', 'elevation', 'battery_level']
    if 'ideal_battery_range_km' in df.columns:
        numeric_columns.append('ideal_battery_range_km')
    elif 'est_battery_range_km' in df.columns:
        numeric_columns.append('est_battery_range_km')
    
    # 添加胎压列 - 使用正确的列名
    tire_columns = ['tpms_pressure_fl', 'tpms_pressure_fr', 'tpms_pressure_rl', 'tpms_pressure_rr']
    for col in tire_columns:
        if col in df.columns:
            numeric_columns.append(col)
    
    # 添加车外温度
    if 'outside_temp' in df.columns:
        numeric_columns.append('outside_temp')
    
    for col in numeric_columns:
        if col in df.columns:
            try:
                # 使用线性插值避免三次样条的问题
                f = interpolate.interp1d(
                    original_timestamps, 
                    df[col].values, 
                    kind='linear', 
                    fill_value="extrapolate",
                    bounds_error=False
                )
                new_df[col] = f(new_timestamps)
                print(f"✅ {col} 线性插值完成")
            except Exception as e:
                print(f"❌ {col}插值失败: {e}")
                # 如果插值失败，使用前向填充
                new_df[col] = np.interp(new_timestamps, original_timestamps, df[col].values)
    
    # 对经纬度进行线性插值
    for col in ['latitude', 'longitude']:
        if col in df.columns:
            try:
                f = interpolate.interp1d(
                    original_timestamps, 
                    df[col].values, 
                    kind='linear', 
                    fill_value="extrapolate",
                    bounds_error=False
                )
                new_df[col] = f(new_timestamps)
            except Exception as e:
                print(f"⚠️  {col}插值失败: {e}")
                # 如果插值失败，使用前向填充
                new_df[col] = np.interp(new_timestamps, original_timestamps, df[col].values)
    
    print(f"✅ 数据插值完成: {len(df)} → {len(new_df)} 个数据点")
    return new_df

def fetch_drive_data(drive_ids):
    """获取行程数据，包含速度、功率、海拔、续航、电量、胎压等"""
    try:
        ids = tuple(int(drive_id) for drive_id in drive_ids) if len(drive_ids) > 1 else f"({int(drive_ids[0])})"
        
        # 检查表结构
        check_sql = """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'positions' 
            AND column_name IN ('elevation', 'ideal_battery_range_km', 'est_battery_range_km', 
                               'tpms_pressure_fl', 'tpms_pressure_fr', 'tpms_pressure_rl', 'tpms_pressure_rr',
                               'outside_temp');
        """
        
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(check_sql)
                available_columns = [row[0] for row in cur.fetchall()]
        
        # 构建动态SQL - 获取所有需要的数据
        base_columns = [
            "date", "latitude", "longitude", "speed", "power", 
            "battery_level"
        ]
        
        if 'elevation' in available_columns:
            base_columns.append("elevation")
        else:
            base_columns.append("NULL as elevation")
        
        # 优先使用ideal_battery_range_km，如果没有则使用est_battery_range_km
        if 'ideal_battery_range_km' in available_columns:
            base_columns.append("ideal_battery_range_km")
        elif 'est_battery_range_km' in available_columns:
            base_columns.append("est_battery_range_km")
        else:
            base_columns.append("NULL as ideal_battery_range_km")
        
        # 添加胎压列 - 使用正确的列名
        tire_columns = ['tpms_pressure_fl', 'tpms_pressure_fr', 'tpms_pressure_rl', 'tpms_pressure_rr']
        for col in tire_columns:
            if col in available_columns:
                base_columns.append(col)
            else:
                base_columns.append(f"NULL as {col}")
        
        # 添加车外温度列
        if 'outside_temp' in available_columns:
            base_columns.append("outside_temp")
        else:
            base_columns.append("NULL as outside_temp")
        
        columns_str = ", ".join(base_columns)
        
        sql = f"""
            SELECT 
                {columns_str}
            FROM positions
            WHERE drive_id IN {ids}
            ORDER BY date ASC;
        """
        
        with connect_db() as conn:
            df = pd.read_sql(sql, conn)
        
        if len(df) == 0:
            print("❌ 未找到行程数据")
            return pd.DataFrame()
        
        # 转换时区 - 兼容有无时区信息的时间戳
        ts = pd.to_datetime(df["date"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize('UTC')
        df["date"] = ts.dt.tz_convert(LOCAL_TIMEZONE)
        
        # 处理海拔数据
        if 'elevation' in available_columns:
            print("🏔️  处理海拔数据中...")
            # 将0或NaN值替换为NaN，然后使用前向填充
            df["elevation"] = df["elevation"].replace(0, np.nan)
            df["elevation"] = df["elevation"].ffill()
            
            # 如果第一行是NaN，则使用后向填充
            df["elevation"] = df["elevation"].bfill()
            
            # 如果仍然有NaN，则填充为0
            df["elevation"] = df["elevation"].fillna(0)
            
            print(f"🏔️  海拔数据范围: {df['elevation'].min():.1f} - {df['elevation'].max():.1f} 米")
        else:
            df["elevation"] = 0
            print("⚠️  数据库中无海拔数据，使用默认值0")
        
        # 处理续航数据 - 使用前向填充方法填充缺失值
        range_column = 'ideal_battery_range_km' if 'ideal_battery_range_km' in df.columns else 'est_battery_range_km'
        if range_column in df.columns:
            print("🔋 处理续航数据中...")
            # 将0或NaN值替换为NaN，然后使用前向填充
            df[range_column] = df[range_column].replace(0, np.nan)
            df[range_column] = df[range_column].ffill()
            
            # 如果第一行是NaN，则使用后向填充
            df[range_column] = df[range_column].bfill()
            
            # 如果仍然有NaN，则填充为0
            df[range_column] = df[range_column].fillna(0)
            
            print(f"🔋 续航数据范围: {df[range_column].min():.1f} - {df[range_column].max():.1f} km")
        else:
            df["ideal_battery_range_km"] = 0
            print("⚠️  数据库中无续航数据，使用默认值0")
        
        # 处理胎压数据 - 使用正确的列名
        tire_columns = ['tpms_pressure_fl', 'tpms_pressure_fr', 'tpms_pressure_rl', 'tpms_pressure_rr']
        for col in tire_columns:
            if col in df.columns:
                print(f"🔄 处理{col}数据中...")
                # 将0或NaN值替换为NaN，然后使用前向填充
                df[col] = df[col].replace(0, np.nan)
                df[col] = df[col].ffill()
                
                # 如果第一行是NaN，则使用后向填充
                df[col] = df[col].bfill()
                
                # 如果仍然有NaN，则填充为0
                df[col] = df[col].fillna(0)
                
                print(f"🔄 {col}数据范围: {df[col].min():.1f} - {df[col].max():.1f} bar")
            else:
                print(f"⚠️  数据库中无{col}数据，使用默认值0")
                df[col] = 0
        
        # 处理车外温度数据
        if 'outside_temp' in available_columns:
            print("🌡️  处理车外温度数据中...")
            df["outside_temp"] = df["outside_temp"].ffill().bfill().fillna(0)
            print(f"🌡️  车外温度范围: {df['outside_temp'].min():.1f} - {df['outside_temp'].max():.1f} °C")
        else:
            df["outside_temp"] = 0
            print("⚠️  数据库中无车外温度数据，使用默认值0")
        
        # 数据插值以提高时间分辨率
        if len(df) >= 4:  # 至少需要4个点才能进行插值
            df = interpolate_data(df)
        else:
            print("⚠️  数据点太少，跳过插值")
        
        return df
        
    except Exception as e:
        print(f"❌ 获取行程数据失败: {e}")
        return pd.DataFrame()

def calculate_distance(lat1, lon1, lat2, lon2):
    """计算两点之间的距离（公里）使用Haversine公式"""
    R = 6371  # 地球半径，单位公里
    
    # 将角度转换为弧度
    lat1_rad = np.radians(lat1)
    lon1_rad = np.radians(lon1)
    lat2_rad = np.radians(lat2)
    lon2_rad = np.radians(lon2)
    
    # Haversine公式
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = np.sin(dlat/2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
    distance = R * c
    
    return distance

def calculate_cumulative_distance(df):
    """计算累积距离 - 修复版本"""
    print("📏 计算本次行程距离...")
    
    # 确保数据按时间排序
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # 初始化累积距离
    cumulative_distance = np.zeros(len(df))
    
    # 计算每两点之间的距离并累积
    valid_points = 0
    for i in range(1, len(df)):
        if (pd.notna(df['latitude'].iloc[i]) and pd.notna(df['longitude'].iloc[i]) and
            pd.notna(df['latitude'].iloc[i-1]) and pd.notna(df['longitude'].iloc[i-1])):
            
            try:
                dist = calculate_distance(
                    df['latitude'].iloc[i-1], df['longitude'].iloc[i-1],
                    df['latitude'].iloc[i], df['longitude'].iloc[i]
                )
                cumulative_distance[i] = cumulative_distance[i-1] + dist
                valid_points += 1
            except Exception as e:
                print(f"⚠️  计算第{i}个点距离时出错: {e}")
                cumulative_distance[i] = cumulative_distance[i-1]
        else:
            cumulative_distance[i] = cumulative_distance[i-1]
    
    df['distance_cumulative'] = cumulative_distance
    
    total_distance = cumulative_distance[-1] if len(cumulative_distance) > 0 else 0
    print(f"📏 本次行程总距离: {total_distance:.2f} km (基于 {valid_points} 个有效GPS点)")
    
    return df

def get_power_color(power):
    """根据功率返回动态颜色（避开绿色区间，防止绿幕扣像误扣）：
    回收（负值）→ 天蓝色（蓝通道主导，安全）
    待机（0附近）→ 白色
    低功率 → 黄色
    中功率 → 橙色
    高功率 → 红色
    """
    if power < -10:
        return '#00BBFF'  # 天蓝，动能回收（B通道主导，扣像安全）
    elif power < 0:
        # -10~0: 天蓝 → 白
        ratio = (power + 10) / 10.0
        r = int(255 * ratio)
        g = int(187 + (255 - 187) * ratio)
        b = 255
        return f'#{r:02X}{g:02X}{b:02X}'
    elif power <= 20:
        return '#FFFFFF'  # 白色，怠速/滑行
    elif power <= 80:
        # 20~80: 黄 → 橙
        ratio = (power - 20) / 60.0
        r = 255
        g = int(255 - 100 * ratio)
        b = 0
        return f'#{r:02X}{g:02X}{b:02X}'
    elif power <= 200:
        # 80~200: 橙 → 红
        ratio = (power - 80) / 120.0
        r = 255
        g = int(155 * (1 - ratio))
        b = 0
        return f'#{r:02X}{g:02X}{b:02X}'
    else:
        return '#FF0000'  # 纯红，高功率

def get_speed_color(speed):
    """根据速度返回动态颜色：静止白色 → 低速黄色 → 中速橙色 → 高速红色
    （完全避开绿色区间，防止绿幕扣像时误扣）
    """
    if speed <= 0:
        return '#FFFFFF'  # 白色，静止
    elif speed <= 60:
        # 0~60: 白 → 黄
        ratio = speed / 60.0
        r = 255
        g = 255
        b = int(255 * (1 - ratio))
        return f'#{r:02X}{g:02X}{b:02X}'
    elif speed <= 120:
        # 60~120: 黄 → 红
        ratio = (speed - 60) / 60.0
        r = 255
        g = int(255 * (1 - ratio))
        b = 0
        return f'#{r:02X}{g:02X}{b:02X}'
    else:
        return '#FF0000'  # 纯红

def create_simple_display(ax, speed, power, elevation, range_km, battery, outside_temp, current_distance, 
                         tire_fl, tire_fr, tire_rl, tire_rr):
    """创建简单的数字显示，包含简化的胎压显示"""
    ax.clear()
    
    # 设置绿色背景
    ax.set_facecolor("green")
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 0.5)  # 恢复原来的Y轴范围
    ax.axis('off')
    
    # 速度颜色动态变化
    speed_color = get_speed_color(speed)
    # 功率颜色动态变化
    power_color = get_power_color(power)
    
    # 定义颜色和位置 - 按顺序：速度、功率、海拔、续航、电量、车外温度、本次行程距离
    # 颜色：红橙黄绿青蓝紫（续航用黄绿#AAFF00，R通道足够高，绿幕扣像安全）
    data_config = [
        {"value": speed,            "label": "速度(km/h)/Speed",   "color": speed_color, "x": 0.9,  "y": 0.02},
        {"value": power,            "label": "功率(kW)/Power",     "color": power_color, "x": 2.2,  "y": 0.02},
        {"value": elevation,        "label": "海拔(m)/Altitude",   "color": "#FFD700",   "x": 3.5,  "y": 0.02},
        {"value": range_km,         "label": "续航(km)/Range",     "color": "#AAFF00",   "x": 4.8,  "y": 0.02},
        {"value": battery,          "label": "电量(%)/Battery",    "color": "#00FFFF",   "x": 6.1,  "y": 0.02},
        {"value": outside_temp,     "label": "气温(℃)/Temp",       "color": "#4169FF",   "x": 7.4,  "y": 0.02},
        {"value": current_distance, "label": "本次行程(km)/Trip",  "color": "#CC66FF",   "x": 8.7,  "y": 0.02},
    ]

    # 描边效果：黑色描边保护所有文字，防止扣像时填充色被误扣导致字体消失
    outline = [patheffects.withStroke(linewidth=4, foreground='black')]

    # 绘制所有数据 - 标签在0，数字在0.02（距离0.02）
    for config in data_config:
        # 显示数值（大字体）- 在标签上方0.02单位
        ax.text(config["x"], config["y"] + 0.02, f"{config['value']:.0f}", color=config["color"],
                ha='center', va='bottom', fontsize=35, weight='bold',
                path_effects=outline)
        # 显示标签（中等字体）
        ax.text(config["x"], config["y"], config["label"], color="white",
                ha='center', va='top', fontsize=14, weight='bold',
                path_effects=outline)
    
    # ==============================
    # 🆕 简化的胎压显示 - 按照指定位置显示
    # ==============================
    
    # 胎压显示位置配置
    tire_positions = {
        'fl': {'x': 6.5, 'y': 0.2},  # 左前
        'fr': {'x': 7.0, 'y': 0.2},  # 右前
        'rl': {'x': 6.5, 'y': 0.15},  # 左后
        'rr': {'x': 7.0, 'y': 0.15}   # 右后
    }
    
    # 胎压数据配置
    tire_data = [
        {"position": tire_positions['fl'], "pressure": tire_fl, "label": "前左/FL"},
        {"position": tire_positions['fr'], "pressure": tire_fr, "label": "前右/FR"},
        {"position": tire_positions['rl'], "pressure": tire_rl, "label": "后左/RL"},
        {"position": tire_positions['rr'], "pressure": tire_rr, "label": "后右/RR"}
    ]
    
    # 绘制胎压数据
    tire_outline = [patheffects.withStroke(linewidth=3, foreground='black')]
    for tire in tire_data:
        pos = tire["position"]
        
        # 根据胎压选择颜色 - 使用与绿色背景对比度高的颜色
        if tire["pressure"] < 2.5:  # 低胎压
            tire_color = "#FF6B6B"  # 亮红色
        elif tire["pressure"] > 3.2:  # 高胎压
            tire_color = "#FFA726"  # 亮橙色
        else:  # 正常胎压
            tire_color = "#00BBFF"  # 天蓝色（替换原青色，蓝通道主导，扣像安全）
        
        # 显示胎压数值
        ax.text(pos['x'], pos['y'], f"{tire['pressure']:.1f}",
                color=tire_color, ha='center', va='center', fontsize=10, weight='bold',
                path_effects=tire_outline)
        
        # 显示轮胎标签
        ax.text(pos['x'], pos['y'] - 0.01, tire["label"],
                color="white", ha='center', va='top', fontsize=8, weight='bold',
                path_effects=tire_outline)

def create_summary_display(ax, stats):
    """创建行程总结显示 - 添加了电池电量消耗信息"""
    ax.clear()
    
    # 设置黑色背景
    ax.set_facecolor("black")
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 9)
    ax.axis('off')

    outline = [patheffects.withStroke(linewidth=4, foreground='black')]
    
    # 标题
    ax.text(5, 8.2, "行程总结 / Trip Summary", color="white", 
            ha='center', va='center', fontsize=36, weight='bold',
            path_effects=outline)
    
    # 统计数据配置
    summary_config = [
        {"label": "行程里程 / Distance",          "value": f"{stats['total_distance']:.1f} km",                                 "y": 7.4, "color": "cyan"},
        {"label": "驾驶时间 / Driving Time",       "value": f"{stats['driving_time']:.1f} min",                                  "y": 6.6, "color": "yellow"},
        {"label": "平均速度 / Avg Speed",          "value": f"{stats['avg_speed']:.1f} km/h",                                    "y": 5.8, "color": "#FFD700"},
        {"label": "净能耗 / Net Energy",           "value": f"{stats['net_energy']:.1f} kWh",                                    "y": 5.0, "color": "orange"},
        {"label": "动能回收 / Regen Energy",       "value": f"{stats['regen_energy']:.1f} kWh",                                  "y": 4.2, "color": "magenta"},
        {"label": "能效 / Efficiency",             "value": f"{stats['efficiency']:.1f} Wh/km",                                  "y": 3.4, "color": "lightblue"},
        {"label": "续航达成率 / Range Achievement","value": f"{stats['range_achievement']:.1f}%",                                 "y": 2.6, "color": "#AAFF00"},
        {"label": "电池电量 / Battery",            "value": f"{stats['start_battery']:.0f}%→→→{stats['end_battery']:.0f}%",      "y": 1.8, "color": "gold"},
        {"label": "预估费用 / Estimated Cost",     "value": f"¥{stats['estimated_cost']:.2f} 元",                                 "y": 1.0, "color": "white"}
    ]
    
    # 绘制统计数据
    for config in summary_config:
        ax.text(3, config["y"], config["label"], color="white",
                ha='right', va='center', fontsize=24, weight='bold',
                path_effects=outline)
        ax.text(7, config["y"], config["value"], color=config["color"],
                ha='left', va='center', fontsize=28, weight='bold',
                path_effects=outline)

def calculate_range_achievement(efficiency):
    """计算续航达成率
    标准能耗为155Wh/km时，达成率为100%
    实际能耗越高，达成率越低
    """
    if efficiency <= 0:
        return 0
    
    # 续航达成率 = (标准能耗 / 实际能耗) * 100%
    achievement = (STANDARD_EFFICIENCY / efficiency) * 100
    
    # 限制最大显示为200%，避免异常值
    return min(achievement, 200)

def calculate_trip_stats(df, merged_info):
    """计算行程统计数据 - 使用TeslaMate的直接能耗数据，移除了胎压统计"""
    # 基本统计
    total_distance = merged_info['total_distance']
    driving_time = merged_info['total_duration']
    
    # 平均速度
    avg_speed = float(np.mean(df['speed'])) if len(df) > 0 else 0
    
    # 直接从TeslaMate获取能耗数据
    net_energy = merged_info['total_net_energy']
    regen_energy = merged_info['total_regen_energy']
    
    # 计算能效（Wh/km）
    efficiency = (net_energy * 1000) / total_distance if total_distance > 0 else 0
    
    # 计算预估费用（基于净能耗和电费单价）
    estimated_cost = net_energy * ELECTRICITY_PRICE
    
    # 计算续航达成率
    range_achievement = calculate_range_achievement(efficiency)
    
    # 获取开始和结束时的电池电量
    start_battery = float(df["battery_level"].iloc[0]) if pd.notna(df["battery_level"].iloc[0]) else 0
    end_battery = float(df["battery_level"].iloc[-1]) if pd.notna(df["battery_level"].iloc[-1]) else 0
    battery_consumption = start_battery - end_battery
    
    return {
        "total_distance": total_distance,
        "driving_time": driving_time,
        "avg_speed": avg_speed,
        "net_energy": net_energy,
        "regen_energy": regen_energy,
        "efficiency": efficiency,
        "estimated_cost": estimated_cost,
        "range_achievement": range_achievement,
        "start_battery": start_battery,
        "end_battery": end_battery,
        "battery_consumption": battery_consumption
    }

# ==============================
# 🎥 主视频渲染函数
# ==============================

def render_drive_video(merged_info):
    """渲染行程视频 - 修复版本"""
    try:
        print("📊 获取行程数据中...")
        df = fetch_drive_data(merged_info["drive_ids"])
        
        if df.empty:
            print("❌ 无法获取行程数据，跳过生成")
            return
        
        print(f"✅ 获取 {len(df)} 条记录，合并行程 {merged_info['drive_ids']}")
        
        # 检查续航数据
        range_column = 'ideal_battery_range_km' if 'ideal_battery_range_km' in df.columns else 'est_battery_range_km'
        if range_column in df.columns:
            print(f"🔋 续航数据范围: {df[range_column].min():.1f} - {df[range_column].max():.1f} km")
        
        if len(df) < 5:
            print("⚠️  数据太少，跳过生成。")
            return

        # 修复累积距离计算
        df = calculate_cumulative_distance(df)
        
        # 计算实际行程时长
        start_time = df['date'].iloc[0]
        end_time = df['date'].iloc[-1]
        actual_duration = (end_time - start_time).total_seconds()
        print(f"⏱️  实际行程时长: {actual_duration/60:.1f} 分钟")
        
        # 计算行程统计数据
        print("📈 计算行程统计数据...")
        trip_stats = calculate_trip_stats(df, merged_info)
        
        # 创建时间轴，确保视频时长与实际行程一致
        total_frames_needed = int(actual_duration * FPS)
        summary_frames = 2 * FPS  # 2秒钟的总结画面
        total_frames = total_frames_needed + summary_frames
        
        print(f"🎞️  需要生成 {total_frames} 帧（{total_frames_needed}帧行程 + {summary_frames}帧总结）")

        # 创建画布 - 简单布局
        fig = plt.figure(figsize=(16, 9), facecolor="green")  # 使用16:9宽高比
        ax = fig.add_subplot(111)
        
        # 进度条 - 使用总帧数
        pbar = tqdm(total=total_frames, desc="🎬 生成视频进度/Rendering Video", unit="帧/frame")

        # =============================
        # 🎞️ 更新函数
        # =============================
        def update(frame_idx):
            # 更新进度条
            pbar.update(1)
            
            # 检查是否进入总结画面
            if frame_idx >= total_frames_needed:
                # 显示总结画面
                create_summary_display(ax, trip_stats)
                return []
            
            # 计算当前帧对应的时间
            current_time = start_time + timedelta(seconds=frame_idx / FPS)
            
            # 找到最接近当前时间的数据点
            time_diffs = (df['date'] - current_time).abs()
            closest_idx = time_diffs.idxmin()
            
            # 当前数据
            try:
                current_speed = float(df["speed"].iloc[closest_idx]) if pd.notna(df["speed"].iloc[closest_idx]) else 0
                current_power = float(df["power"].iloc[closest_idx]) if pd.notna(df["power"].iloc[closest_idx]) else 0
                current_elevation = float(df["elevation"].iloc[closest_idx]) if pd.notna(df["elevation"].iloc[closest_idx]) else 0
                current_battery = float(df["battery_level"].iloc[closest_idx]) if pd.notna(df["battery_level"].iloc[closest_idx]) else 0
                current_distance = float(df["distance_cumulative"].iloc[closest_idx]) if pd.notna(df["distance_cumulative"].iloc[closest_idx]) else 0
                
                # 获取车外温度
                current_outside_temp = float(df["outside_temp"].iloc[closest_idx]) if "outside_temp" in df.columns and pd.notna(df["outside_temp"].iloc[closest_idx]) else 0
                
                # 获取续航数据
                if range_column in df.columns:
                    current_range = float(df[range_column].iloc[closest_idx]) if pd.notna(df[range_column].iloc[closest_idx]) else 0
                else:
                    current_range = 0
                
                # 获取胎压数据 - 使用正确的列名
                tire_fl = float(df["tpms_pressure_fl"].iloc[closest_idx]) if pd.notna(df["tpms_pressure_fl"].iloc[closest_idx]) else 0
                tire_fr = float(df["tpms_pressure_fr"].iloc[closest_idx]) if pd.notna(df["tpms_pressure_fr"].iloc[closest_idx]) else 0
                tire_rl = float(df["tpms_pressure_rl"].iloc[closest_idx]) if pd.notna(df["tpms_pressure_rl"].iloc[closest_idx]) else 0
                tire_rr = float(df["tpms_pressure_rr"].iloc[closest_idx]) if pd.notna(df["tpms_pressure_rr"].iloc[closest_idx]) else 0

                # 更新显示（包含简化的胎压显示）
                create_simple_display(ax, current_speed, current_power, current_elevation, 
                                     current_range, current_battery, current_outside_temp, current_distance, 
                                     tire_fl, tire_fr, tire_rl, tire_rr)
                
            except Exception as e:
                print(f"⚠️  更新第{frame_idx}帧时出错: {e}")
                # 如果出错，显示默认值
                create_simple_display(ax, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

            return []

        # =============================
        # 🎬 动画保存
        # =============================
        print("🚀 开始生成动画...")
        
        try:
            # 创建动画 - 使用总帧数
            ani = FuncAnimation(fig, update, frames=total_frames, 
                               interval=1000/FPS, blit=False, repeat=False)
            
            output_path = os.path.join(OUTPUT_DIR, f"drive_merged_{merged_info['drive_ids'][0]}.mp4")
            
            ani.save(output_path, writer="ffmpeg", fps=FPS, dpi=100, 
                     savefig_kwargs={'facecolor':'green'})
            
            # 确保进度条完成
            if pbar.n < total_frames:
                pbar.update(total_frames - pbar.n)
            
            print(f"🎬 视频已生成/Video generated：{output_path}")
            print(f"📊 行程统计/Trip Stats: {merged_info['total_distance']:.1f}km, {merged_info['total_duration']:.1f}分钟/min")
            print(f"🎞️ 视频信息/Video Info: {total_frames}帧/frames, {total_frames/FPS:.1f}秒/sec, {FPS}帧/秒/fps")
            print("\n📈 行程详细统计/Detailed Trip Stats:")
            print(f"   行程里程/Distance: {trip_stats['total_distance']:.1f} km")
            print(f"   驾驶时间/Driving Time: {trip_stats['driving_time']:.1f} min")
            print(f"   平均速度/Average Speed: {trip_stats['avg_speed']:.1f} km/h")
            print(f"   净能耗/Net Energy: {trip_stats['net_energy']:.1f} kWh")
            print(f"   动能回收/Regen Energy: {trip_stats['regen_energy']:.1f} kWh")
            print(f"   能效/Efficiency: {trip_stats['efficiency']:.1f} Wh/km")
            print(f"   续航达成率/Range Achievement: {trip_stats['range_achievement']:.1f}% (标准: {STANDARD_EFFICIENCY}Wh/km)")
            print(f"   电池电量/Battery: {trip_stats['start_battery']:.0f}% →→→ {trip_stats['end_battery']:.0f}% (消耗: {trip_stats['battery_consumption']:.1f}%)")
            print(f"   预估费用/Estimated Cost: ¥{trip_stats['estimated_cost']:.2f} 元 (电费: {ELECTRICITY_PRICE}元/度)")
            
        except Exception as e:
            print(f"❌ 视频生成失败/Video generation failed: {e}")
        finally:
            pbar.close()
            plt.close(fig)
        
    except Exception as e:
        print(f"❌ 渲染视频过程中出错: {e}")

# ==============================
# 🏁 主程序入口
# ==============================
if __name__ == "__main__":
    print("🚗 TeslaMate 行程可视化工具/TeslaMate Drive Visualizer")
    print("=" * 60)
    
    # 显示电费设置和标准能耗
    print(f"⚡ 当前电费设置: {ELECTRICITY_PRICE} 元/度")
    print(f"🎯 标准能耗参考: {STANDARD_EFFICIENCY} Wh/km (Model 3 Performance 1:1达成率)")
    
    # 获取车辆列表
    print("🚘 获取车辆列表中...")
    cars = get_cars()
    
    if cars.empty:
        print("❌ 未找到车辆数据")
        exit()
    
    print("\n🚘 车辆列表/Vehicle List:")
    print("-" * 50)
    for i, car in cars.iterrows():
        print(f"{i}: {car['name']} (VIN: {car['vin'][-6:]}, 效率: {car['efficiency']:.3f} kWh/km)")
    
    try:
        car_selected = int(input("\n🎯 请选择车辆序号/Select vehicle number: "))
        if 0 <= car_selected < len(cars):
            selected_car = cars.iloc[car_selected]
            print(f"✅ 已选择车辆: {selected_car['name']}")
            
            # 获取行程数据（包含地址）
            print("📋 获取行程列表中/Fetching drives...")
            drives = get_drives_with_addresses(selected_car['id'], 20)
            
            if drives.empty:
                print("❌ 未找到行程数据/No drive data found")
                exit()
            
            # 合并连续行程
            merged_groups = merge_continuous_drives(drives.reset_index(drop=True))
            
            print("\n📋 最近行程列表/Recent Drives List:")
            print("-" * 80)
            
            for i, group in enumerate(merged_groups):
                merged_info = get_merged_drive_info(group)
                if merged_info:
                    # 计算预估费用和续航达成率
                    estimated_cost = merged_info['total_net_energy'] * ELECTRICITY_PRICE
                    efficiency = (merged_info['total_net_energy'] * 1000) / merged_info['total_distance'] if merged_info['total_distance'] > 0 else 0
                    range_achievement = calculate_range_achievement(efficiency)
                    
                    start_time = merged_info['start_date'].strftime('%Y-%m-%d %H:%M')
                    end_time = merged_info['end_date'].strftime('%H:%M') if pd.notna(merged_info['end_date']) else "进行中/Ongoing"
                    
                    print(f"{i}: {start_time} → {end_time}")
                    print(f"   行程/Route: {merged_info['start_address']} → {merged_info['end_address']}")
                    print(f"   距离/Distance: {merged_info['total_distance']:.1f}km, 时长/Duration: {merged_info['total_duration']:.1f}分钟/min")
                    print(f"   能耗/Energy: {merged_info['total_net_energy']:.1f}kWh使用, {merged_info['total_regen_energy']:.1f}kWh回收")
                    print(f"   能效/Efficiency: {efficiency:.1f} Wh/km")
                    print(f"   续航达成率/Range Achievement: {range_achievement:.1f}%")
                    print(f"   预估费用/Estimated Cost: ¥{estimated_cost:.2f} 元")
                    print(f"   包含行程数/Drives included: {len(merged_info['drive_ids'])}")
                    print()
            
            try:
                selected = int(input("🎯 请输入要生成的行程组序号/Enter drive group number: "))
                if 0 <= selected < len(merged_groups):
                    merged_info = get_merged_drive_info(merged_groups[selected])
                    if merged_info:
                        print(f"🚀 开始生成行程视频: {merged_info['start_address']} → {merged_info['end_address']}")
                        render_drive_video(merged_info)
                    else:
                        print("❌ 无法获取行程信息/Cannot get drive info")
                else:
                    print("❌ 输入无效/Invalid input")
            except ValueError:
                print("❌ 请输入有效数字/Please enter a valid number")
        else:
            print("❌ 输入无效/Invalid input")
    except ValueError:
        print("❌ 请输入有效数字/Please enter a valid number")
    except Exception as e:
        print(f"❌ 程序运行出错: {e}")
