from models.backbone import *
from models.backbone.helper import *
from models.backbone.baseblock_US import *
from models.backbone.baseblock import *
from models.BaseModel import BaseModel
import utils.GIOU as GIOUloss

class StrongerV3_US(BaseModel):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.activate_type = 'relu6'
        self.headslarge = nn.Sequential(OrderedDict([
            ('conv0',USconv_bn(self.outC[0], 512, kernel=1, stride=1, padding=0)),
            ('conv1', USsepconv_bn(512, 1024, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv2',USconv_bn(1024, 512, kernel=1, stride=1, padding=0)),
            ('conv3', USsepconv_bn(512, 1024, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv4',USconv_bn(1024, 512, kernel=1, stride=1, padding=0)),
        ]))
        self.detlarge = nn.Sequential(OrderedDict([
            ('conv5', USsepconv_bn(512, 1024, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv6', USconv_bias(1024, self.gt_per_grid * (self.numclass + 5), kernel=1, stride=1, padding=0,us=[True,False]))
        ]))
        self.mergelarge = nn.Sequential(OrderedDict([
            # ('conv7',USconv_bn(512, 256, kernel=1, stride=1, padding=0)),
            ('conv7',USconv_bn(512, self.outC[1], kernel=1, stride=1, padding=0)),
            ('upsample0', nn.UpsamplingNearest2d(scale_factor=2)),
        ]))
        # -----------------------------------------------
        self.headsmid = nn.Sequential(OrderedDict([
            ('conv8',USconv_bn(self.outC[1], 256, kernel=1, stride=1, padding=0)),
            ('conv9', USsepconv_bn(256, 512, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv10',USconv_bn(512, 256, kernel=1, stride=1, padding=0)),
            ('conv11', USsepconv_bn(256, 512, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv12',USconv_bn(512, 256, kernel=1, stride=1, padding=0)),
        ]))
        self.detmid = nn.Sequential(OrderedDict([
            ('conv13', USsepconv_bn(256, 512, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv14', USconv_bias(512, self.gt_per_grid * (self.numclass + 5), kernel=1, stride=1, padding=0,us=[True,False]))
        ]))
        self.mergemid = nn.Sequential(OrderedDict([
            # ('conv15',USconv_bn(256, 128, kernel=1, stride=1, padding=0)),
            ('conv15',USconv_bn(256, self.outC[2], kernel=1, stride=1, padding=0)),
            ('upsample0', nn.UpsamplingNearest2d(scale_factor=2)),
        ]))
        # -----------------------------------------------
        self.headsmall = nn.Sequential(OrderedDict([
            # ('conv16',USconv_bn(self.outC[2]+128, 128, kernel=1, stride=1, padding=0)),
            ('conv16',USconv_bn(self.outC[2], 128, kernel=1, stride=1, padding=0)),
            ('conv17', USsepconv_bn(128, 256, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv18',USconv_bn(256, 128, kernel=1, stride=1, padding=0)),
            ('conv19', USsepconv_bn(128, 256, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv20',USconv_bn(256, 128, kernel=1, stride=1, padding=0)),
        ]))
        self.detsmall = nn.Sequential(OrderedDict([
            ('conv21', USsepconv_bn(128, 256, kernel=3, stride=1, padding=1, seprelu=cfg.seprelu)),
            ('conv22', USconv_bias(256, self.gt_per_grid * (self.numclass + 5), kernel=1, stride=1, padding=0,us=[True,False]))
        ]))
        if cfg.ASFF:
            self.ASFF_US0 = ASFF_US(0, activate=self.activate_type)
            self.ASFF_US1 = ASFF_US(1, activate=self.activate_type)
            self.ASFF_US2 = ASFF_US(2, activate=self.activate_type)
        self.apply(lambda m: setattr(m, 'width_mult',1.0))
    def forward(self, input, targets=None):
        self.input_size = input.shape[-1]
        feat_small, feat_mid, feat_large = self.backbone(input)
        conv = self.headslarge(feat_large)
        convlarge = conv

        conv = self.mergelarge(convlarge)
        conv = self.headsmid(conv+feat_mid)
        convmid = conv

        conv = self.mergemid(convmid)
        conv = self.headsmall(conv+feat_small)
        convsmall = conv
        if self.cfg.ASFF:
            convlarge = self.asff0(convlarge, convmid, convsmall)
            convmid = self.asff1(convlarge, convmid, convsmall)
            convsmall = self.asff2(convlarge, convmid, convsmall)
        outlarge = self.detlarge(convlarge)
        outmid = self.detmid(convmid)
        outsmall = self.detsmall(convsmall)
        if self.training:
            assert targets is not None
            predlarge = self.decode(outlarge, 32)
            predmid = self.decode(outmid, 16)
            predsmall = self.decode(outsmall, 8)
            return self.loss([predsmall, predmid, predlarge], targets)
        else:
            predlarge = self.decode_infer(outlarge, 32)
            predmid = self.decode_infer(outmid, 16)
            predsmall = self.decode_infer(outsmall, 8)
            pred = torch.cat([predsmall, predmid, predlarge], dim=1)
            return pred

    def build_target(self, bboxs: list, preds):
        # get target for each image
        batch_targets = []
        batch_preds = []
        for idx_img in range(preds[0].shape[0]):
            batch_preds.append(torch.cat([p[idx_img] for p in preds], 0))
        for bbox, pred in zip(bboxs, batch_preds):
            batch_targets.append(self.yolo_target_single(bbox, pred))
        batch_targets = torch.stack(batch_targets, 0)
        return batch_targets

    def yolo_target_single(self, target: torch.Tensor, pred: torch.Tensor):

        bbox, class_label, mix_weight = target.split([4, 1, 1], dim=1)
        class_label = class_label.long()
        reg_area_limit = [0, 30, 90, 10000]
        strides = [8, 16, 32]
        target_lvl = [torch.zeros(self.input_size // strides[i], self.input_size // strides[i], self.gt_per_grid,
                                  6 + self.numclass).cuda() for i in range(3)]
        target_count = [torch.zeros(self.input_size // strides[i], self.input_size // strides[i]).long() for i in
                        range(3)]
        bbox_xywh = torch.cat([(bbox[:, 2:] + bbox[:, :2]) * 0.5,
                               bbox[:, 2:] - bbox[:, :2]], dim=-1)
        bboxarea = torch.sqrt(bbox_xywh[:, -2] * bbox_xywh[:, -1])
        for i in range(3):
            # initialize box weight 1
            target_lvl[i][:, :, :, 5] = 1.0
            valid_mask = (bboxarea > reg_area_limit[i]) & (bboxarea < reg_area_limit[i + 1])
            for gt_xywh, class_index, gt_xyxy, box_weight in zip(bbox_xywh[valid_mask], class_label[valid_mask],
                                                                 bbox[valid_mask], mix_weight[valid_mask]):
                gt_xywh = (gt_xywh / strides[i]).long()
                numgt = target_count[i][gt_xywh[1]][gt_xywh[0]]
                delta = 0.01
                if numgt==0 and self.gt_per_grid>1:
                    for n in range(self.gt_per_grid):
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][:4] = gt_xyxy
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][4] = 1.0
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][5] = box_weight
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][6:] = 1.0 / self.numclass * delta
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][6 + class_index] = (1.0 - delta) + 1.0 / self.numclass * delta
                else:
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][:4] = gt_xyxy
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][4] = 1.0
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][5] = box_weight
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][6:] = 1.0 / self.numclass * delta
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][6 + class_index] = (1.0 - delta) + 1.0 / self.numclass * delta
                target_count[i][gt_xywh[1]][gt_xywh[0]] = min(self.gt_per_grid - 1,target_count[i][gt_xywh[1]][gt_xywh[0]] + 1)
        target_lvl = [t.view(-1, 6 + self.numclass) for t in target_lvl]
        target_lvl = torch.cat(target_lvl, 0)
        iou = GIOUloss.bbox_overlaps(pred[:, :4], target[:, :4])
        max_iou, _ = torch.max(iou, dim=-1)
        max_iou = max_iou.unsqueeze(-1)
        respond_bgd = (torch.ones_like(target_lvl[:, 4:5]) - target_lvl[:, 4:5]) * (max_iou < 0.5).float()
        # respond_bgd = (torch.ones_like(target_lvl[:,4:5])- target_lvl[:,4:5])
        target_lvl = torch.cat([target_lvl, respond_bgd], -1)
        return target_lvl

class StrongerV3_US_dummy(BaseModel):
    def __init__(self,cfg):
        super().__init__(cfg)
        self.activate_type = 'relu6'
        self.headslarge=nn.Sequential(OrderedDict([
            ('conv0',conv_bn(self.outC[0],512,kernel=1,stride=1,padding=0)),
            ('conv1', sepconv_bn(512, 1024, kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv2', conv_bn(1024, 512, kernel=1,stride=1,padding=0)),
            ('conv3', sepconv_bn(512, 1024, kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv4', conv_bn(1024, 512, kernel=1,stride=1,padding=0)),
        ]))
        self.detlarge=nn.Sequential(OrderedDict([
            ('conv5',sepconv_bn(512,1024,kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv6', conv_bias(1024, self.gt_per_grid*(self.numclass+5),kernel=1,stride=1,padding=0))
        ]))
        self.mergelarge=nn.Sequential(OrderedDict([
            ('conv7',conv_bn(512,self.outC[1],kernel=1,stride=1,padding=0)),
            ('upsample0',nn.UpsamplingNearest2d(scale_factor=2)),
        ]))
        #-----------------------------------------------
        self.headsmid=nn.Sequential(OrderedDict([
            ('conv8',conv_bn(self.outC[1],256,kernel=1,stride=1,padding=0)),
            ('conv9', sepconv_bn(256, 512, kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv10', conv_bn(512, 256, kernel=1,stride=1,padding=0)),
            ('conv11', sepconv_bn(256, 512, kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv12', conv_bn(512, 256, kernel=1,stride=1,padding=0)),
        ]))
        self.detmid=nn.Sequential(OrderedDict([
            ('conv13',sepconv_bn(256,512,kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv14', conv_bias(512, self.gt_per_grid*(self.numclass+5),kernel=1,stride=1,padding=0))
        ]))
        self.mergemid=nn.Sequential(OrderedDict([
            ('conv15',conv_bn(256,self.outC[2],kernel=1,stride=1,padding=0)),
            ('upsample0',nn.UpsamplingNearest2d(scale_factor=2)),
        ]))
        #-----------------------------------------------
        self.headsmall=nn.Sequential(OrderedDict([
            ('conv16',conv_bn(self.outC[2],128,kernel=1,stride=1,padding=0)),
            ('conv17', sepconv_bn(128, 256, kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv18', conv_bn(256, 128, kernel=1,stride=1,padding=0)),
            ('conv19', sepconv_bn(128, 256, kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv20', conv_bn(256, 128, kernel=1,stride=1,padding=0)),
        ]))
        self.detsmall=nn.Sequential(OrderedDict([
            ('conv21',sepconv_bn(128,256,kernel=3, stride=1, padding=1,seprelu=cfg.seprelu)),
            ('conv22', conv_bias(256, self.gt_per_grid*(self.numclass+5),kernel=1,stride=1,padding=0))
        ]))
        if cfg.ASFF:
            self.asff0 = ASFF(0, activate=self.activate_type)
            self.asff1 = ASFF(1, activate=self.activate_type)
            self.asff2 = ASFF(2, activate=self.activate_type)
    def forward(self, input, targets=None):
        self.input_size = input.shape[-1]
        feat_small, feat_mid, feat_large = self.backbone(input)
        conv = self.headslarge(feat_large)
        convlarge = conv

        conv = self.mergelarge(convlarge)
        conv = self.headsmid(conv+feat_mid)
        convmid = conv

        conv = self.mergemid(convmid)
        conv = self.headsmall(conv+feat_small)
        convsmall = conv
        if self.cfg.ASFF:
            convlarge = self.asff0(convlarge, convmid, convsmall)
            convmid = self.asff1(convlarge, convmid, convsmall)
            convsmall = self.asff2(convlarge, convmid, convsmall)
        outlarge = self.detlarge(convlarge)
        outmid = self.detmid(convmid)
        outsmall = self.detsmall(convsmall)
        if self.training:
            assert targets is not None
            predlarge = self.decode(outlarge, 32)
            predmid = self.decode(outmid, 16)
            predsmall = self.decode(outsmall, 8)
            return self.loss([predsmall, predmid, predlarge], targets)
        else:
            predlarge = self.decode_infer(outlarge, 32)
            predmid = self.decode_infer(outmid, 16)
            predsmall = self.decode_infer(outsmall, 8)
            pred = torch.cat([predsmall, predmid, predlarge], dim=1)
            return pred

    def build_target(self, bboxs: list, preds):
        # get target for each image
        batch_targets = []
        batch_preds = []
        for idx_img in range(preds[0].shape[0]):
            batch_preds.append(torch.cat([p[idx_img] for p in preds], 0))
        for bbox, pred in zip(bboxs, batch_preds):
            batch_targets.append(self.yolo_target_single(bbox, pred))
        batch_targets = torch.stack(batch_targets, 0)
        return batch_targets

    def yolo_target_single(self, target: torch.Tensor, pred: torch.Tensor):

        bbox, class_label, mix_weight = target.split([4, 1, 1], dim=1)
        class_label = class_label.long()
        reg_area_limit = [0, 30, 90, 10000]
        strides = [8, 16, 32]
        target_lvl = [torch.zeros(self.input_size // strides[i], self.input_size // strides[i], self.gt_per_grid,
                                  6 + self.numclass).cuda() for i in range(3)]
        target_count = [torch.zeros(self.input_size // strides[i], self.input_size // strides[i]).long() for i in
                        range(3)]
        bbox_xywh = torch.cat([(bbox[:, 2:] + bbox[:, :2]) * 0.5,
                               bbox[:, 2:] - bbox[:, :2]], dim=-1)
        bboxarea = torch.sqrt(bbox_xywh[:, -2] * bbox_xywh[:, -1])
        for i in range(3):
            # initialize box weight 1
            target_lvl[i][:, :, :, 5] = 1.0
            valid_mask = (bboxarea > reg_area_limit[i]) & (bboxarea < reg_area_limit[i + 1])
            for gt_xywh, class_index, gt_xyxy, box_weight in zip(bbox_xywh[valid_mask], class_label[valid_mask],
                                                                 bbox[valid_mask], mix_weight[valid_mask]):
                gt_xywh = (gt_xywh / strides[i]).long()
                numgt = target_count[i][gt_xywh[1]][gt_xywh[0]]
                delta = 0.01
                if numgt==0 and self.gt_per_grid>1:
                    for n in range(self.gt_per_grid):
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][:4] = gt_xyxy
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][4] = 1.0
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][5] = box_weight
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][6:] = 1.0 / self.numclass * delta
                        target_lvl[i][gt_xywh[1]][gt_xywh[0]][n][6 + class_index] = (1.0 - delta) + 1.0 / self.numclass * delta
                else:
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][:4] = gt_xyxy
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][4] = 1.0
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][5] = box_weight
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][6:] = 1.0 / self.numclass * delta
                    target_lvl[i][gt_xywh[1]][gt_xywh[0]][numgt][6 + class_index] = (1.0 - delta) + 1.0 / self.numclass * delta
                target_count[i][gt_xywh[1]][gt_xywh[0]] = min(self.gt_per_grid - 1,target_count[i][gt_xywh[1]][gt_xywh[0]] + 1)
        target_lvl = [t.view(-1, 6 + self.numclass) for t in target_lvl]
        target_lvl = torch.cat(target_lvl, 0)
        iou = GIOUloss.bbox_overlaps(pred[:, :4], target[:, :4])
        max_iou, _ = torch.max(iou, dim=-1)
        max_iou = max_iou.unsqueeze(-1)
        respond_bgd = (torch.ones_like(target_lvl[:, 4:5]) - target_lvl[:, 4:5]) * (max_iou < 0.5).float()
        # respond_bgd = (torch.ones_like(target_lvl[:,4:5])- target_lvl[:,4:5])
        target_lvl = torch.cat([target_lvl, respond_bgd], -1)
        return target_lvl

if __name__ == '__main__':
    import torch.onnx

    # net=YoloV3(20)
    net = StrongerV3_US(0)
    load_tf_weights(net, 'cocoweights-half.pkl')

    assert 0
    model = net.eval()
    load_checkpoint(model, torch.load('checkpoints/coco512_prune/checkpoint-best.pth'))
    statedict = model.state_dict()
    layer2block = defaultdict(list)
    for k, v in model.state_dict().items():
        if 'num_batches_tracked' in k:
            statedict.pop(k)
    for idx, (k, v) in enumerate(statedict.items()):
        if 'mobilev2' in k:
            continue
        else:
            flag = k.split('.')[1]
            layer2block[flag].append((k, v))
    for k, v in layer2block.items():
        print(k, len(v))
    pruneratio = 0.1

    # #onnx
    # input = torch.randn(1, 3, 320, 320)
    # torch.onnx.export(net, input, "coco320.onnx", verbose=True)
    # #onnxcheck
    # model=onnx.load("coco320.onnx")
    # onnx.checker.check_model(model)
