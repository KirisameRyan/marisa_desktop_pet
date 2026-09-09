# 由 tools_parse_shimeji.py 从 Shimeji 動作.xml 生成，勿手改。
# pose: [图片, tick数, 位移x(负=朝左), 位移y, 锚点x, 锚点y]
ACTIONS = {
    '振り向く': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
        ],
    },
    '変位': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
        ],
    },
    '立つ': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime1.png', 250, 0, 0, 64, 128],
            ]},
        ],
    },
    '歩く': {
        'kind': '移動',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime1.png', 6, -2, 0, 64, 128],
                ['shime2.png', 6, -2, 0, 64, 128],
                ['shime1.png', 6, -2, 0, 64, 128],
                ['shime3.png', 6, -2, 0, 64, 128],
            ]},
        ],
    },
    '走る': {
        'kind': '移動',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime52.png', 8, -4, 0, 64, 128],
                ['shime50.png', 4, -4, 0, 64, 128],
                ['shime51.png', 12, -4, 0, 64, 128],
                ['shime50.png', 4, -4, 0, 64, 128],
                ['shime52.png', 4, -4, 0, 64, 128],
            ]},
        ],
    },
    '猛ダッシュ': {
        'kind': '移動',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime52.png', 8, -8, 0, 64, 128],
                ['shime50.png', 4, -8, 0, 64, 128],
                ['shime51.png', 12, -8, 0, 64, 128],
                ['shime50.png', 4, -8, 0, 64, 128],
                ['shime52.png', 4, -8, 0, 64, 128],
            ]},
        ],
    },
    '座る': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime11.png', 250, 0, 0, 64, 112],
            ]},
        ],
    },
    '座って見上げる': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime26.png', 250, 0, 0, 64, 112],
            ]},
        ],
    },
    '座ってマウスを見上げる': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': '#{mascot.environment.cursor.y < mascot.environment.screen.height/2}', 'poses': [
                ['shime26.png', 250, 0, 0, 64, 112],
            ]},
            {'cond': None, 'poses': [
                ['shime11.png', 250, 0, 0, 64, 112],
            ]},
        ],
    },
    '座って首が回る': {
        'kind': '固定',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime26.png', 5, 0, 0, 64, 112],
                ['shime15.png', 10, 0, 0, 64, 112],
                ['shime27.png', 30, 0, 0, 64, 112],
                ['shime15.png', 10, 0, 0, 64, 112],
                ['shime16.png', 5, 0, 0, 64, 112],
                ['shime28.png', 5, 0, 0, 64, 112],
                ['shime17.png', 5, 0, 0, 64, 112],
                ['shime29.png', 5, 0, 0, 64, 112],
                ['shime11.png', 5, 0, 0, 64, 112],
            ]},
        ],
    },
    '楽に座る': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime30.png', 250, 0, 0, 64, 112],
            ]},
        ],
    },
    '足を下ろして座る': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime31.png', 250, 0, 0, 64, 112],
            ]},
        ],
    },
    '足をぶらぶらさせる': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime31.png', 5, 0, 0, 64, 112],
                ['shime32.png', 15, 0, 0, 64, 112],
                ['shime31.png', 5, 0, 0, 64, 112],
                ['shime33.png', 15, 0, 0, 64, 112],
            ]},
        ],
    },
    '寝そべる': {
        'kind': '静止',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime21.png', 250, 0, 0, 64, 128],
            ]},
        ],
    },
    'ずりずり': {
        'kind': '移動',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime20.png', 28, 0, 0, 64, 128],
                ['shime20.png', 4, -2, 0, 64, 128],
                ['shime21.png', 4, -2, 0, 64, 128],
                ['shime21.png', 4, -1, 0, 64, 128],
                ['shime21.png', 24, 0, 0, 64, 128],
            ]},
        ],
    },
    '天井に掴まる': {
        'kind': '静止',
        'frame': '天井',
        'anims': [
            {'cond': None, 'poses': [
                ['shime25.png', 10, 0, 0, 64, 48],
                ['shime23.png', 10, 0, 0, 64, 48],
                ['shime24.png', 30, 0, 0, 64, 48],
                ['shime23.png', 10, 0, 0, 64, 48],
                ['shime25.png', 10, 0, 0, 64, 48],
            ]},
        ],
    },
    '天井を伝う': {
        'kind': '移動',
        'frame': '天井',
        'anims': [
            {'cond': None, 'poses': [
                ['shime25.png', 8, -4, 0, 64, 48],
                ['shime23.png', 4, -4, 0, 64, 48],
                ['shime24.png', 12, -4, 0, 64, 48],
                ['shime23.png', 4, -4, 0, 64, 48],
                ['shime25.png', 4, -4, 0, 64, 48],
            ]},
        ],
    },
    '壁に掴まる': {
        'kind': '静止',
        'frame': '壁',
        'anims': [
            {'cond': None, 'poses': [
                ['shime14.png', 250, 0, 0, 64, 128],
            ]},
        ],
    },
    '壁を登る': {
        'kind': '移動',
        'frame': '壁',
        'anims': [
            {'cond': '#{目的地Y < mascot.anchor.y}', 'poses': [
                ['shime14.png', 16, 0, 0, 64, 128],
                ['shime14.png', 4, 0, -1, 64, 128],
                ['shime12.png', 4, 0, -1, 64, 128],
                ['shime13.png', 4, 0, -1, 64, 128],
                ['shime13.png', 16, 0, 0, 64, 128],
                ['shime13.png', 4, 0, -2, 64, 128],
                ['shime12.png', 4, 0, -2, 64, 128],
                ['shime14.png', 4, 0, -2, 64, 128],
            ]},
            {'cond': '#{目的地Y >= mascot.anchor.y}', 'poses': [
                ['shime14.png', 16, 0, 0, 64, 128],
                ['shime14.png', 4, 0, 2, 64, 128],
                ['shime12.png', 4, 0, 2, 64, 128],
                ['shime13.png', 4, 0, 2, 64, 128],
                ['shime13.png', 16, 0, 0, 64, 128],
                ['shime13.png', 4, 0, 1, 64, 128],
                ['shime12.png', 4, 0, 1, 64, 128],
                ['shime14.png', 4, 0, 1, 64, 128],
            ]},
        ],
    },
    'IEを持って落ちる': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime47.png', 250, 0, 0, 64, 128],
            ]},
        ],
    },
    'IEを持って歩く': {
        'kind': '組み込み',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime34.png', 6, -2, 0, 64, 128],
                ['shime35.png', 6, -2, 0, 64, 128],
                ['shime34.png', 6, -2, 0, 64, 128],
                ['shime36.png', 6, -2, 0, 64, 128],
            ]},
        ],
    },
    'IEを持って走る': {
        'kind': '組み込み',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime34.png', 2, -8, 0, 64, 128],
                ['shime35.png', 2, -8, 0, 64, 128],
                ['shime34.png', 2, -8, 0, 64, 128],
                ['shime36.png', 2, -8, 0, 64, 128],
            ]},
        ],
    },
    'IEを投げる': {
        'kind': '組み込み',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime37.png', 50, 0, 0, 64, 128],
            ]},
        ],
    },
    'ジャンプ': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime22.png', 250, 0, 0, 64, 128],
            ]},
        ],
    },
    '落ちる': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime53.png', 250, 0, 0, 64, 128],
            ]},
        ],
    },
    '跳ねる': {
        'kind': '固定',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime18.png', 10, 0, 0, 64, 128],
                ['shime19.png', 5, 0, 0, 64, 128],
                ['shime49.png', 5, 0, 0, 64, 128],
                ['shime19.png', 5, 0, 0, 64, 128],
                ['shime49.png', 5, 0, 0, 64, 128],
            ]},
        ],
    },
    '転ぶ': {
        'kind': '固定',
        'frame': '地面',
        'anims': [
            {'cond': None, 'poses': [
                ['shime53.png', 3, 0, 0, 64, 128],
                ['shime54.png', 3, 0, 0, 64, 128],
                ['shime53.png', 3, 0, 0, 64, 128],
                ['shime54.png', 3, 0, 0, 64, 128],
                ['shime53.png', 3, 0, 0, 64, 128],
                ['shime54.png', 3, 0, 0, 64, 128],
                ['shime53.png', 3, 0, 0, 64, 128],
                ['shime54.png', 3, 0, 0, 64, 128],
                ['shime18.png', 10, 0, 0, 64, 128],
                ['shime19.png', 5, 0, 0, 64, 128],
                ['shime49.png', 5, 0, 0, 64, 128],
                ['shime19.png', 5, 0, 0, 64, 128],
                ['shime49.png', 5, 0, 0, 64, 128],
            ]},
        ],
    },
    'つままれる': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': '#{footX < mascot.environment.cursor.x-50}', 'poses': [
                ['shime9.png', 5, 0, 0, 64, 128],
            ]},
            {'cond': '#{footX < mascot.environment.cursor.x-30}', 'poses': [
                ['shime7.png', 5, 0, 0, 64, 128],
            ]},
            {'cond': '#{footX < mascot.environment.cursor.x+30}', 'poses': [
                ['shime48.png', 5, 0, 0, 64, 128],
            ]},
            {'cond': '#{footX < mascot.environment.cursor.x+50}', 'poses': [
                ['shime8.png', 5, 0, 0, 64, 128],
            ]},
            {'cond': '#{footX >= mascot.environment.cursor.x+30}', 'poses': [
                ['shime10.png', 5, 0, 0, 64, 128],
            ]},
        ],
    },
    '抵抗する': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime48.png', 50, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime48.png', 100, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 5, 0, 0, 64, 128],
                ['shime6.png', 5, 0, 0, 64, 128],
                ['shime5.png', 2, 0, 0, 64, 128],
                ['shime6.png', 2, 0, 0, 64, 128],
                ['shime5.png', 2, 0, 0, 64, 128],
                ['shime6.png', 2, 0, 0, 64, 128],
                ['shime5.png', 2, 0, 0, 64, 128],
                ['shime6.png', 2, 0, 0, 64, 128],
                ['shime5.png', 2, 0, 0, 64, 128],
                ['shime6.png', 2, 0, 0, 64, 128],
            ]},
        ],
    },
    '落下する': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ドラッグされる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '投げられる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '立ってボーっとする': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '座ってボーっとする': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '寝そべってボーっとする': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '座って足をぶらぶらさせる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '壁に掴まってボーっとする': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '壁から落ちる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '天井に掴まってボーっとする': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '天井から落ちる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺を歩く': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺を走る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺でずりずり': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺の左の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺の右の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺から左の壁によじのぼる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの下辺から右の壁によじのぼる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってワークエリアの下辺の左の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってワークエリアの下辺の右の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってワークエリアの下辺から左の壁によじのぼる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってワークエリアの下辺から右の壁によじのぼる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの下に飛びつく': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの壁を途中まで登る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの壁を登る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'ワークエリアの上辺を伝う': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井を歩く': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井を走る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井でずりずり': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井の左の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井の右の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井の左の端っこから飛び降りる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの天井の右の端っこから飛び降りる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってIEの天井の左の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってIEの天井の右の端っこで座る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってIEの天井の左の端っこから飛び降りる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってIEの天井の右の端っこから飛び降りる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '猛ダッシュでIEの天井の左の端っこから飛び降りる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '猛ダッシュでIEの天井の右の端っこから飛び降りる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの壁を途中まで登る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの壁を登る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの下辺を伝う': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの下辺から左の壁によじのぼる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの下辺から右の壁によじのぼる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '左の壁に飛びつく': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '右の壁に飛びつく': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの左に飛びつく': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEの右に飛びつく': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEを右に投げる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'IEを左に投げる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってIEを右に投げる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '走ってIEを左に投げる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    'マウスの周りに集まる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '座ってマウスのほうを見る': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '座ってマウスのほうを見てたら首が回った': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '引っこ抜く1': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime1.png', 16, 0, 0, 64, 128],
                ['shime38.png', 40, 0, 0, 64, 128],
                ['shime39.png', 40, 0, 0, 64, 128],
                ['shime40.png', 40, 0, 0, 64, 128],
                ['shime41.png', 40, 0, 0, 64, 128],
            ]},
        ],
    },
    '引っこ抜く2': {
        'kind': '固定',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime9.png', 1, 20, -20, 32, 128],
                ['shime9.png', 1, 20, -10, 32, 128],
                ['shime9.png', 1, 20, -5, 32, 128],
            ]},
        ],
    },
    '引っこ抜く': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '引っこ抜かれる': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '分裂1': {
        'kind': '組み込み',
        'frame': '',
        'anims': [
            {'cond': None, 'poses': [
                ['shime42.png', 30, 0, 0, 64, 128],
                ['shime43.png', 10, 0, 0, 64, 128],
                ['shime44.png', 3, 0, 0, 64, 128],
                ['shime45.png', 3, 0, 0, 64, 128],
                ['shime46.png', 10, 0, 0, 64, 128],
            ]},
        ],
    },
    '分裂する': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
    '分裂した': {
        'kind': '複合',
        'frame': '',
        'anims': [
        ],
    },
}
