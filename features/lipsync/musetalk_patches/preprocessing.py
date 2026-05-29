import sys
from face_detection import FaceAlignment,LandmarksType
from os import listdir, path
import subprocess
import numpy as np
import cv2
import pickle
import os
import json
import face_alignment as _face_alignment_pkg
import torch
from tqdm import tqdm

# Landmark backend: face-alignment (68-point dlib layout) replaces mmpose/DWPose.
# mmcv/mmpose has no Blackwell (sm_120) wheels and won't build on Windows; the
# 68-point face_alignment output is index-compatible with the mmpose wholebody
# face keypoints MuseTalk used (nose-bridge indices 28-30 line up).
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_LMK_FA = _face_alignment_pkg.FaceAlignment(
    _face_alignment_pkg.LandmarksType.TWO_D, flip_input=False,
    device=("cuda" if torch.cuda.is_available() else "cpu"),
)


def _get_face68(img_bgr):
    """Return (68,2) int face landmarks for the most prominent face, or None."""
    preds = _LMK_FA.get_landmarks_from_image(img_bgr[:, :, ::-1])  # BGR -> RGB
    if not preds:
        return None
    best = max(preds, key=lambda lm: (lm[:, 0].max() - lm[:, 0].min()) * (lm[:, 1].max() - lm[:, 1].min()))
    return best.astype(np.int32)

# initialize the face detection model
device = "cuda" if torch.cuda.is_available() else "cpu"
fa = FaceAlignment(LandmarksType._2D, flip_input=False,device=device)

# maker if the bbox is not sufficient 
coord_placeholder = (0.0,0.0,0.0,0.0)

def resize_landmark(landmark, w, h, new_w, new_h):
    w_ratio = new_w / w
    h_ratio = new_h / h
    landmark_norm = landmark / [w, h]
    landmark_resized = landmark_norm * [new_w, new_h]
    return landmark_resized

def read_imgs(img_list):
    frames = []
    print('reading images...')
    for img_path in tqdm(img_list):
        frame = cv2.imread(img_path)
        frames.append(frame)
    return frames

def get_bbox_range(img_list,upperbondrange =0):
    frames = read_imgs(img_list)
    batch_size_fa = 1
    batches = [frames[i:i + batch_size_fa] for i in range(0, len(frames), batch_size_fa)]
    coords_list = []
    landmarks = []
    if upperbondrange != 0:
        print('get key_landmark and face bounding boxes with the bbox_shift:',upperbondrange)
    else:
        print('get key_landmark and face bounding boxes with the default value')
    average_range_minus = []
    average_range_plus = []
    for fb in tqdm(batches):
        face_land_mark = _get_face68(np.asarray(fb)[0])
        if face_land_mark is None:
            coords_list += [coord_placeholder]
            continue

        # get bounding boxes by face detetion
        bbox = fa.get_detections_for_batch(np.asarray(fb))
        
        # adjust the bounding box refer to landmark
        # Add the bounding box to a tuple and append it to the coordinates list
        for j, f in enumerate(bbox):
            if f is None: # no face in the image
                coords_list += [coord_placeholder]
                continue
            
            half_face_coord =  face_land_mark[29]#np.mean([face_land_mark[28], face_land_mark[29]], axis=0)
            range_minus = (face_land_mark[30]- face_land_mark[29])[1]
            range_plus = (face_land_mark[29]- face_land_mark[28])[1]
            average_range_minus.append(range_minus)
            average_range_plus.append(range_plus)
            if upperbondrange != 0:
                half_face_coord[1] = upperbondrange+half_face_coord[1] #手动调整  + 向下（偏29）  - 向上（偏28）

    text_range=f"Total frame:「{len(frames)}」 Manually adjust range : [ -{int(sum(average_range_minus) / len(average_range_minus))}~{int(sum(average_range_plus) / len(average_range_plus))} ] , the current value: {upperbondrange}"
    return text_range
    

def get_landmark_and_bbox(img_list,upperbondrange =0):
    frames = read_imgs(img_list)
    batch_size_fa = 1
    batches = [frames[i:i + batch_size_fa] for i in range(0, len(frames), batch_size_fa)]
    coords_list = []
    landmarks = []
    if upperbondrange != 0:
        print('get key_landmark and face bounding boxes with the bbox_shift:',upperbondrange)
    else:
        print('get key_landmark and face bounding boxes with the default value')
    average_range_minus = []
    average_range_plus = []
    for fb in tqdm(batches):
        face_land_mark = _get_face68(np.asarray(fb)[0])
        if face_land_mark is None:
            coords_list += [coord_placeholder]
            continue

        # Derive the face box straight from the 68 landmarks. The vendored SFD
        # detector (fa.get_detections_for_batch) is a HUMAN face detector and
        # misses stylized/creature faces -- gating on it dropped ~96% of frames
        # on a cat video, so the output was near-frozen (27 frames of 776) and
        # looked like "no lip sync". face-alignment already located the face, so
        # use its landmarks for the box and never gate on SFD.
        half_face_coord = face_land_mark[29].astype(float).copy()
        range_minus = (face_land_mark[30] - face_land_mark[29])[1]
        range_plus  = (face_land_mark[29] - face_land_mark[28])[1]
        average_range_minus.append(range_minus)
        average_range_plus.append(range_plus)
        if upperbondrange != 0:
            half_face_coord[1] = upperbondrange + half_face_coord[1]
        half_face_dist = np.max(face_land_mark[:, 1]) - half_face_coord[1]
        upper_bond = max(0, half_face_coord[1] - half_face_dist)

        f_landmark = (
            int(np.min(face_land_mark[:, 0])), int(upper_bond),
            int(np.max(face_land_mark[:, 0])), int(np.max(face_land_mark[:, 1])),
        )
        x1, y1, x2, y2 = f_landmark
        if y2 - y1 <= 0 or x2 - x1 <= 0 or x1 < 0:
            coords_list += [coord_placeholder]   # degenerate box -> keep frame as-is
        else:
            coords_list += [f_landmark]
    
    print("********************************************bbox_shift parameter adjustment**********************************************************")
    print(f"Total frame:「{len(frames)}」 Manually adjust range : [ -{int(sum(average_range_minus) / len(average_range_minus))}~{int(sum(average_range_plus) / len(average_range_plus))} ] , the current value: {upperbondrange}")
    print("*************************************************************************************************************************************")
    return coords_list,frames
    

if __name__ == "__main__":
    img_list = ["./results/lyria/00000.png","./results/lyria/00001.png","./results/lyria/00002.png","./results/lyria/00003.png"]
    crop_coord_path = "./coord_face.pkl"
    coords_list,full_frames = get_landmark_and_bbox(img_list)
    with open(crop_coord_path, 'wb') as f:
        pickle.dump(coords_list, f)
        
    for bbox, frame in zip(coords_list,full_frames):
        if bbox == coord_placeholder:
            continue
        x1, y1, x2, y2 = bbox
        crop_frame = frame[y1:y2, x1:x2]
        print('Cropped shape', crop_frame.shape)
        
        #cv2.imwrite(path.join(save_dir, '{}.png'.format(i)),full_frames[i][0][y1:y2, x1:x2])
    print(coords_list)
