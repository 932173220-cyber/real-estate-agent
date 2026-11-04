"""Generated business fixtures: 20 communities, 200 listings, 10 customers.

Each community gets six topic documents (概况/交通/教育/医疗/商业/居住体验),
so a chunk never straddles two subjects and every chunk can carry its own
community and section. Numbers are deterministic, not random per run.
"""
from __future__ import annotations

REGIONS = ('滨江区', '新城区', '老城区', '高新区', '城东新区')

# key, name, region, delivered, floors, elevator, metro station, walk metres
COMMUNITIES = (
    ('CW', '澄湾雅苑', '滨江区', 2018, 18, True, '澄江路站', 450),
    ('YT', '云庭花园', '滨江区', 2020, 22, True, '云庭路站', 800),
    ('YA', '云岸新城', '新城区', 2021, 25, True, '云岸大道站', 620),
    ('QT', '栖庭里', '老城区', 2004, 6, False, '栖庭路站', 650),
    ('SY', '松屿公馆', '新城区', 2022, 18, True, '松屿站', 1600),
    ('HX', '和溪花园', '滨江区', 2007, 7, False, '和溪路站', 900),
    ('JL', '锦澜湾', '高新区', 2016, 20, True, '锦澜路站', 380),
    ('BL', '白鹭洲', '城东新区', 2019, 26, True, '白鹭洲站', 540),
    ('QS', '青杉里', '老城区', 1999, 6, False, '青杉路站', 420),
    ('XD', '西堤春晓', '滨江区', 2015, 16, True, '西堤站', 700),
    ('LY', '兰屿台', '新城区', 2023, 28, True, '兰屿站', 950),
    ('HY', '湖悦湾', '高新区', 2017, 24, True, '湖悦站', 300),
    ('CZ', '翠竹苑', '老城区', 2001, 7, False, '翠竹路站', 780),
    ('JL2', '嘉林公馆', '城东新区', 2020, 21, True, '嘉林站', 460),
    ('TY', '听雨轩', '滨江区', 2012, 14, True, '听雨路站', 620),
    ('MC', '墨池坊', '老城区', 1996, 6, False, '墨池站', 350),
    ('HY2', '海月台', '高新区', 2022, 30, True, '海月站', 880),
    ('SF', '松風里', '新城区', 2014, 17, True, '松風站', 520),
    ('BX', '碧溪庄园', '城东新区', 2011, 12, True, '碧溪站', 1050),
    ('YX', '云溪九里', '滨江区', 2023, 26, True, '云溪站', 410),
)

SCHOOLS = {
    '滨江区': ('滨江实验小学', '滨江第二中学', '青岚幼儿园'),
    '新城区': ('新城外国语小学', '新城实验中学', '云朵幼儿园'),
    '老城区': ('老城中心小学', '栖霞中学', '小树苗幼儿园'),
    '高新区': ('高新一小', '高新实验中学', '锦鲤幼儿园'),
    '城东新区': ('城东实验小学', '白鹭中学', '青禾幼儿园'),
}
HOSPITALS = {
    '滨江区': ('滨江社区卫生服务中心', '滨江医院'),
    '新城区': ('新城社区卫生服务站', '新城人民医院'),
    '老城区': ('老城街道卫生服务中心', '市第二医院'),
    '高新区': ('高新门诊部', '高新医院'),
    '城东新区': ('城东社区诊所', '城东医院'),
}
MALLS = {
    '滨江区': ('滨江生活广场', '澄江路商业街'),
    '新城区': ('新城万达', '云岸汇'),
    '老城区': ('老城百货', '栖霞路菜场'),
    '高新区': ('高新天地', '锦澜里商业街'),
    '城东新区': ('城东印象城', '白鹭洲广场'),
}
PARKS = ('滨江公园', '云栖绿地', '老城文化广场', '高新中央公园', '城东湿地公园')

ORIENTATIONS = ('南', '南北', '东南', '西南', '东', '西')
DECORATIONS = ('精装', '简装', '毛坯')


def community_rows():
    rows = []
    for index, (key, name, region, year, floors, elevator, station, walk) in enumerate(COMMUNITIES):
        school, middle, kindergarten = SCHOOLS[region]
        clinic, hospital = HOSPITALS[region]
        mall, street = MALLS[region]
        rows.append(dict(key=key, name=name, region=region, year=year, floors=floors, elevator=elevator,
                         station=station, walk=walk, school=school, middle=middle, kindergarten=kindergarten,
                         clinic=clinic, hospital=hospital, mall=mall, street=street,
                         # Not every community is strong in every dimension; that
                         # is what makes a retrieval query discriminate.
                         has_school=index % 4 != 3, has_hospital_near=index % 3 != 2,
                         has_mall_near=index % 5 != 4, quiet=index % 6 != 5,
                         park=PARKS[index % len(PARKS)]))
    return rows


def topic_documents():
    """(filename, metadata, text) for every community topic."""
    documents = []
    for row in community_rows():
        prefix = f"{row['key']}-{row['name']}"
        year = row['year']
        elevator = '统一配置电梯' if row['elevator'] else '以多层为主，未统一配置电梯'
        documents.append((f'{prefix}-概况.txt', dict(title=row['name'], section='概况'),
                          f"{row['name']}位于{row['region']}，约{year}年交付，共{row['floors']}层，{elevator}。"
                          f"小区出入口设有门禁与访客登记，车位以地面与地下混合方式配置，物业费按建筑面积计收。"
                          f"楼栋之间留有步行通道，公共区域包含垃圾分类点、快递柜与儿童活动场地。"
                          f"户型以两室和三室为主，部分楼栋带独立储藏间。"
                          f"房屋建成时间较早的楼栋，管线与外墙维护情况需要按楼栋单独核对。"))
        documents.append((f'{prefix}-交通.txt', dict(title=row['name'], section='交通'),
                          f"轨道交通方面，{row['station']}位于小区周边，步行距离按{row['walk']}米记录，"
                          f"不同楼栋与出入口的实际路线需要现场核对。门口有公交站点，可接驳主干道与地铁站。"
                          f"自驾出行主要经{row['region']}干道，早晚高峰车流集中，部分路口存在排队。"
                          f"小区地面车位与地下车位比例有限，访客车辆需登记；非机动车棚靠近各楼栋入口。"))
        documents.append((f'{prefix}-教育.txt', dict(title=row['name'], section='教育'),
                          (f"周边分布有{row['kindergarten']}、{row['school']}与{row['middle']}。"
                           f"幼儿园与小学步行可达，中学通常需要公交或家长接送。"
                           if row['has_school'] else
                           f"小区周边以{row['kindergarten']}为主，片区内小学需要跨片区就读，最近的小学距离约两公里，"
                           f"中学通学通常依赖公交。")
                          + f"招生范围以当年教育部门公布的政策为准，距离近不代表具备入学资格，"
                            f"具体批次、落户年限与学位占用情况需要单独核实。"
                            f"上下学时段周边道路人流与车流集中，通学路线需按实际出入口规划。"))
        documents.append((f'{prefix}-医疗.txt', dict(title=row['name'], section='医疗'),
                          f"日常就医可前往{row['clinic']}，提供全科门诊、慢病随访与预防保健服务，步行约十分钟。"
                          + (f"综合诊疗可前往{row['hospital']}，通常需要公交或驾车，急诊时段建议提前确认路线。"
                             if row['has_hospital_near'] else
                             f"片区综合医院距离约三公里，需要公交或驾车前往，夜间急诊建议提前规划路线。")
                          + f"小区周边设有药店与口腔门诊，夜间购药选择有限。"
                            f"老年人居住较多的楼栋，需要考虑就医交通与无障碍通行条件。"))
        documents.append((f'{prefix}-商业.txt', dict(title=row['name'], section='商业'),
                          f"日常生活采买以{row['street']}为主，包含生鲜店、便利店、早餐铺与药店。"
                          + (f"较大规模的购物与餐饮集中在{row['mall']}，步行或一站公交可达。"
                             if row['has_mall_near'] else
                             f"较大规模的购物与餐饮需要前往{row['mall']}，驾车约十分钟，公交班次有限。")
                          + f"小区底商以生活服务类为主，夜间营业时间不一，靠近商业街的楼栋需要关注夜间噪声。"
                            f"周末采购高峰时段，周边道路与停车场压力较大。"))
        documents.append((f'{prefix}-居住体验.txt', dict(title=row['name'], section='居住体验'),
                          f"小区内部以步行为主，{row['park']}距离较近，适合日常散步与慢跑。"
                          f"绿化维护情况按物业投入而异，部分楼栋低层采光受树木与相邻楼体影响。"
                          + ("临街楼栋较少，早晚噪声主要来自小区内部活动。"
                             if row['quiet'] else
                             "临主干道楼栋受车流噪声影响，建议现场确认窗户隔音。")
                          + f"靠设备层与电梯井的房间需要关注运行噪声。"
                            f"租户与自住比例会影响社区氛围，具体以实地观察与物业信息为准。"
                            f"停车紧张程度在工作日晚间最明显，建议按实际车位情况判断。"))
    return documents


LEGACY = (
    ('P001', 'CW', 358, 109.0, 3, 8, 18, True, 450),
    ('P002', 'YT', 398, 118.0, 3, 12, 22, True, 800),
    ('P003', 'CW', 338, 106.0, 3, 1, 18, True, 470),
    ('P004', 'YA', 420, 128.0, 3, 9, 25, True, 620),
    ('P005', 'QT', 268, 82.0, 2, 3, 6, False, 650),
    ('P006', 'QT', 375, 112.0, 3, 3, 6, False, 700),
    ('P007', 'YT', 399, 115.0, 3, 16, 22, True, 300),
    ('P008', 'SY', 296, 101.0, 3, 6, 18, True, 1600),
    ('P009', 'HX', 328, 103.0, 3, 5, 7, False, 900),
    ('P010', 'HX', 348, 107.0, 3, 2, 7, False, 920),
    ('P011', 'YA', 386, 121.0, 3, 7, 25, True, 1100),
    ('P012', 'SY', 258, 78.0, 2, 9, 18, True, 1400),
    ('P013', 'CW', 468, 138.0, 4, 10, 18, True, 430),
    ('P014', 'YT', 288, 85.0, 2, 7, 22, True, 350),
    ('P015', 'YA', 398, 119.0, 3, 1, 25, True, 550),
    ('P016', 'SY', 308, 104.0, 3, 11, 18, True, None),
)


def listing_rows():
    """200 listings: the original sixteen IDs stay stable, ten per community."""
    catalogue = {row['key']: row for row in community_rows()}
    rows = []
    for property_id, key, price_wan, area, rooms, floor, total, elevator, metro in LEGACY:
        community = catalogue[key]
        rows.append(_listing(property_id, community, price_wan * 10000, area, rooms, floor, total, elevator, metro))
    for community in community_rows():
        existing = [row for row in rows if row['community_key'] == community['key']]
        for index in range(len(existing), 10):
            price_per_sqm = {'滨江区': 38000, '新城区': 33000, '老城区': 28000, '高新区': 40000, '城东新区': 31000}[community['region']]
            area = (78.0, 88.0, 95.0, 102.0, 108.0, 112.0, 118.0, 125.0, 132.0, 140.0)[index]
            price_wan = int(price_per_sqm * area / 10000) + (index - 4) * 4
            floor = min(community['floors'], 2 + index * 2)
            rows.append(_listing(f"{community['key']}-{index + 1:02d}", community, price_wan * 10000, area,
                                 2 if area < 95 else (3 if area < 132 else 4), floor, community['floors'],
                                 community['elevator'], community['walk'] + (index - 5) * 40))
    return rows


def _listing(property_id, community, price_yuan, area, rooms, floor, total, elevator, metro):
    seq = sum(ord(char) for char in property_id)
    return dict(property_id=property_id, community_key=community['key'], community_name=community['name'],
                region=community['region'], price_yuan=price_yuan, area_sqm=area, bedrooms=rooms, floor=floor,
                total_floors=total, orientation=ORIENTATIONS[seq % len(ORIENTATIONS)],
                decoration=DECORATIONS[seq % len(DECORATIONS)], building_age=max(0, 2026 - community['year']),
                has_elevator=elevator, metro_distance_m=metro,
                intro=(f"{community['name']}约{area:g}平方米{rooms}室，位于{floor}/{total}层，"
                       f"周边有{community['school']}与{community['clinic']}，步行到{community['station']}约{metro}米。"),
                is_demo=True, source='')


CUSTOMERS = (
    ('fixture-lin', '林女士', '关注三室、教育配套与地铁通勤，希望避开一层。',
     {'max_price_yuan': 4200000, 'bedrooms': 3, 'min_floor': 2, 'prefer_near_metro': True}),
    ('fixture-chen', '陈先生', '预算三百万以内的两室，需要电梯。',
     {'max_price_yuan': 3000000, 'bedrooms': 2, 'require_elevator': True}),
    ('fixture-zhou', '周先生', '改善型三室，重视小区环境与停车。',
     {'max_price_yuan': 5200000, 'bedrooms': 3, 'min_area_sqm': 110}),
    ('fixture-wu', '吴女士', '老城区两室，接受无电梯但要求低楼层。',
     {'max_price_yuan': 2600000, 'bedrooms': 2, 'max_floor': 3}),
    ('fixture-zheng', '郑先生', '高新区通勤，预算四百万左右，最好靠近地铁。',
     {'max_price_yuan': 4000000, 'max_metro_distance_m': 600}),
    ('fixture-sun', '孙女士', '三室带书房，重视教育配套，可接受毛坯。',
     {'max_price_yuan': 4800000, 'bedrooms': 3, 'education_priority': True}),
    ('fixture-he', '何先生', '首次置业，预算两百六十万以内，面积八十平左右。',
     {'max_price_yuan': 2600000, 'max_area_sqm': 90}),
    ('fixture-luo', '罗女士', '关注医疗配套，希望离医院近，楼层不要太高。',
     {'max_price_yuan': 3500000, 'max_floor': 10}),
    ('fixture-gao', '高先生', '投资兼自住，关注租金与交通，预算三百五十万。',
     {'max_price_yuan': 3500000, 'prefer_near_metro': True}),
    ('fixture-xu', '徐女士', '四室改善，预算五百五十万以内，需要电梯与南向。',
     {'max_price_yuan': 5500000, 'bedrooms': 4, 'require_elevator': True, 'orientation': '南'}),
)

