import argparse
import os
from datetime import datetime, timedelta

import cv2
from ultralytics import YOLO

KEEP = {'person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck', 'dog', 'cat'}


def group_events(hits, gap):
    events = []
    for label, t, conf, idx in hits:
        for ev in reversed(events):
            if ev['label'] == label and t - ev['end'] <= gap:
                ev['end'] = t
                ev['count'] += 1
                if conf > ev['conf']:
                    ev['conf'] = conf
                    ev['frame'] = idx
                break
        else:
            events.append({'label': label, 'start': t, 'end': t, 'conf': conf, 'frame': idx, 'count': 1})
    return events


def clock(base, seconds):
    if not base:
        return '{:02d}:{:02d}'.format(int(seconds) // 60, int(seconds) % 60)
    return (base + timedelta(seconds=seconds)).strftime('%H:%M:%S')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video')
    ap.add_argument('--model', default='yolo11n.pt')
    ap.add_argument('--stride', type=int, default=5)
    ap.add_argument('--conf', type=float, default=0.35)
    ap.add_argument('--start', default='')
    ap.add_argument('--gap', type=float, default=3.0)
    ap.add_argument('--outdir', default='')
    args = ap.parse_args()

    base = datetime.strptime(args.start, '%Y-%m-%d %H:%M:%S') if args.start else None

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit('cannot open ' + args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    model = YOLO(args.model)
    names = model.names

    hits = []
    idx = 0
    scanned = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % args.stride == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            scanned += 1
            res = model.predict(frame, conf=args.conf, verbose=False)[0]
            for box in res.boxes:
                label = names[int(box.cls)]
                if label in KEEP:
                    hits.append((label, idx / fps, float(box.conf), idx))
        idx += 1
    cap.release()

    events = group_events(hits, args.gap)
    events.sort(key=lambda e: e['start'])

    print('video      : {}'.format(args.video))
    print('frames     : {} total, {} scanned (every {})'.format(total, scanned, args.stride))
    print('model      : {}  conf>={}'.format(args.model, args.conf))
    print('detections : {} boxes in {} events\n'.format(len(hits), len(events)))

    counts = {}
    for ev in events:
        counts[ev['label']] = counts.get(ev['label'], 0) + 1
    print('by class   : ' + (', '.join('{}={}'.format(k, v) for k, v in sorted(counts.items())) or 'none'))
    print()

    for n, ev in enumerate(events, 1):
        print('{:>3}. {:<11} {} -> {}  ({:.0f}s, peak {:.2f}, {} hits)'.format(
            n, ev['label'], clock(base, ev['start']), clock(base, ev['end']),
            ev['end'] - ev['start'], ev['conf'], ev['count']))

    if args.outdir and events:
        os.makedirs(args.outdir, exist_ok=True)
        cap = cv2.VideoCapture(args.video)
        for n, ev in enumerate(events, 1):
            cap.set(cv2.CAP_PROP_POS_FRAMES, ev['frame'])
            ok, frame = cap.read()
            if not ok:
                continue
            res = model.predict(frame, conf=args.conf, verbose=False)[0]
            path = os.path.join(args.outdir, 'ev{:02d}-{}-{:.2f}.jpg'.format(n, ev['label'], ev['conf']))
            cv2.imwrite(path, res.plot())
        cap.release()
        print('\nannotated peaks -> {}'.format(args.outdir))


if __name__ == '__main__':
    main()
