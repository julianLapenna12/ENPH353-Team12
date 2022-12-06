from geometry_msgs.msg import Twist
import rospy
import cv2
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import Image
import sys
import numpy as np
from time import sleep

from hsv_view import ImageProcessor
from model import Model
from scrape_frames import DataScraper
from plate_reader import PlateReader
from pull_plate import PlatePull
from copy import deepcopy
import time
from std_msgs.msg import String

plate_dir = "/home/fizzer/ros_ws/src/ENPH353-Team12/src/plate_temp2"

class Driver:
    DEF_VALS = (0.5, 0.5)
    MODEL_PATH = "/home/fizzer/ros_ws/src/models/drive_model-0.h5"
    INNER_MOD_PATH = "/home/fizzer/ros_ws/src/models/inner-drive-model-0.0.h5"
    """
    (0.5,0) = 0
    (0, -1) = 1
    (0, 1) = 2
    (0.5, -1) = 3
    (0.5, 1) = 4
    """
    ONE_HOT = { 
        0 : (DataScraper.SET_X, 0),
        1 : (0, -1*DataScraper.SET_Z),
        2 : (0, DataScraper.SET_Z),
        3 : (DataScraper.SET_X, -1*DataScraper.SET_Z),
        4 : (DataScraper.SET_X, DataScraper.SET_Z)
    }
    FPS = 20
    ROWS = 720
    COLS = 1280
    """crosswalk"""
    CROSSWALK_FRONT_AREA_THRES = 5000
    CROSSWALK_BACK_AREA_THRES = 400
    CROSSWALK_MSE_STOPPED_THRES = 9
    CROSSWALK_MSE_MOVING_THRES = 40
    DRIVE_PAST_CROSSWALK_FRAMES = int(FPS*3)
    FIRST_STOP_SECS = 1
    CROSSWALK_X = 0.4
    """LP"""
    SLOW_DOWN_AREA_LOWER = 9000
    SLOW_DOWN_AREA_UPPER = 60000
    SLOW_DOWN_AREA_FRAMES = 5  # consecutive

    SLOW_DOWN_X = 0.07
    SLOW_DOWN_Z = 0.55
    """transition"""
    STRAIGHT_DEGS_THRES = 0.3
    RED_INTERSEC_PIX = 445
    RED_INTERSEC_PIX_THRES = 5

    """Outside loop control"""
    NUM_CROSSWALK_STOP = 2
    OUTSIDE_LOOP_SECS = 10 

    """Turn to inside intersec"""
    BLUE_AREA_THRES_TURN = 10000
    """
    TODOS:
    - increase LP accuracy - mixing LA26 with LX26
    - inner loop: find ways to be perpendicular with red line, build CNN
    """
    def __init__(self):
        """Creates a Driver object. Responsible for driving the robot throughout the track. 
        """            
        self.twist_pub = rospy.Publisher('/R1/cmd_vel', Twist, queue_size=1)
        self.image_sub = rospy.Subscriber("/R1/pi_camera/image_raw", Image, self.callback_img)
        self.license_pub = rospy.Publisher("/license_plate", String, queue_size=1)
        self.move = Twist()
        self.bridge = CvBridge()

        self.move.linear.x = 0
        self.move.angular.z = 0

        self.dv_mod = Model(Driver.MODEL_PATH)
        self.inner_dv_mod = Model(Driver.INNER_MOD_PATH)
        self.pr = PlateReader(script_run=False)

        self.is_stopped_crosswalk = False
        self.first_ped_moved = False
        self.first_ped_stopped = False
        self.prev_mse_frame = None
        self.crossing_crosswalk_count = 0
        self.is_crossing_crosswalk = False
        self.first_stopped_frames_count = 0

        self.at_plate = False
        self.plate_frames_count = 0
        self.plate_drive_back = False
        self.drive_back_frames_count = 0
        self.num_fast_frames = 0
        
        self.lp_dict = {}
        self.id_dict = {}
        self.id_stats_dict = {}

        self.num_crosswalks = 0
        self.start = time.time()
        self.curr_t = self.start
        self.outside_ended = False
        self.acquire_lp = False
        self.first_crosswalk_stop = True

        self.start_seq_state = True
        self.start_counter = 0
        self.update_preds_state = False
        self.in_transition = False
        self.turning_transition = False
        self.inner_loop = False

    def callback_img(self, data):
        """Callback function for the subscriber node for the /image_raw ros topic. 
        This callback is called when a new message has arrived to the /image_raw topic (i.e. a new frame from the camera).
        Using the image, it conducts the following:

        1) drives and looks for a red line (if not crossing the crosswalk)
        2) if a red line is seen, stops the robot
        3) drives past the red line when pedestrian is not crossing
        
        Args:
            data (sensor_msgs::Image): The image recieved from the robot's camera
        """        
        # cv_image = self.bridge.imgmsg_to_cv2(data, desired_encoding='passthrough')
        
        if self.start_seq_state:
            self.start_seq()
            return

        cv_image = self.bridge.imgmsg_to_cv2(data, "bgr8")
        if self.inner_loop:
            hsv = DataScraper.process_img(cv_image, type="bgr")
            predicted = self.dv_mod.predict(hsv)
            pred_ind = np.argmax(predicted)
            self.move.linear.x = Driver.ONE_HOT[pred_ind][0]
            self.move.angular.z = Driver.ONE_HOT[pred_ind][1]
            self.twist_pub.publish(self.move)
            return
        elif self.turning_transition:
            print("turning transition")
            # turn until blue area thres
            z = 1
            x = 0
            crped = ImageProcessor.crop(cv_image, row_start=int(720/2.2))
            blu_crped = ImageProcessor.filter_blue(crped)
            largest_blu_area = ImageProcessor.contours_area(blu_crped)[0]
            print("largest blue area", largest_blu_area)
            if largest_blu_area and largest_blu_area > Driver.BLUE_AREA_THRES_TURN:
                z = 0
                self.turning_transition = False
                self.inner_loop = True
            self.move.linear.x = x
            self.move.angular.z = z
            self.twist_pub.publish(self.move)
            return
        elif self.in_transition:
            # straighten
            z_st, x_st = self.is_straightened(cv_image)
            z = 0
            x = 0
            z = -1.0*z_st / 10
            x = -1.0*x_st / 5
            self.move.angular.z = z
            if z == 0:
                self.move.linear.x = x
            if x_st == 0 and z_st == 0:
                self.in_transition = False
                self.turning_transition = True
            print("------------Move-------------",self.move.linear.x, self.move.angular.z)
            self.twist_pub.publish(self.move)
            return
        elif self.update_preds_state and self.outside_ended:
            print("TIME", self.curr_t - self.start)
            min_prob = 0.5
            self.post_process_preds()      
            print("\n\n")
            print("PLATE RESULTS")
            print(self.get_plate_results())
            print("\n\n")
            print("PRINTING STATS")
            self.print_stats()
            self.in_transition = True
            self.update_preds_state = False
            return
        self.curr_t = time.time()
        if (self.curr_t - self.start) > Driver.OUTSIDE_LOOP_SECS and self.num_crosswalks >= Driver.NUM_CROSSWALK_STOP and self.is_stopped_crosswalk:
            self.outside_ended = True
            self.update_preds_state = True
            self.move.linear.x = 0
            self.move.linear.z = 0
            self.twist_pub.publish(self.move)
            return 
        if self.is_stopped_crosswalk:
            if self.first_crosswalk_stop:
                self.num_crosswalks += 1
                self.first_crosswalk_stop = False
            print("stopped crosswalk")
            if self.can_cross_crosswalk(cv_image):
                print("can cross")
                self.is_stopped_crosswalk = False
                self.prev_mse_frame = None
                self.first_ped_stopped = False
                self.first_ped_moved = False
                self.is_crossing_crosswalk = True
                self.first_crosswalk_stop = True
                # self.num_crosswalks += 1
            return

        hsv = DataScraper.process_img(cv_image, type="bgr")

        predicted = self.dv_mod.predict(hsv)
        pred_ind = np.argmax(predicted)
        self.move.linear.x = Driver.ONE_HOT[pred_ind][0]
        self.move.angular.z = Driver.ONE_HOT[pred_ind][1]
        
        r_st = int(Driver.ROWS/2.5)
        crped = ImageProcessor.crop(cv_image, row_start=r_st)
        blu_area = PlatePull.get_contours_area(ImageProcessor.filter(crped, ImageProcessor.blue_low, ImageProcessor.blue_up))
        print("Blue area:", blu_area)

        if blu_area and blu_area[0] > Driver.SLOW_DOWN_AREA_LOWER and blu_area[0] < Driver.SLOW_DOWN_AREA_UPPER or self.num_fast_frames < Driver.SLOW_DOWN_AREA_FRAMES:
            # x = round(self.move.linear.x/5, 6) 
            # z = round(self.move.angular.z/1.5, 6)
            x = round(self.move.linear.x, 6) 
            z = round(self.move.angular.z, 6)
            if x > 0:
                x = Driver.SLOW_DOWN_X
            elif x < 0:
                x = -1*Driver.SLOW_DOWN_X
            if z > 0:
                z = Driver.SLOW_DOWN_Z
            elif z < 0:
                z = -1*Driver.SLOW_DOWN_Z

            self.move.linear.x = x
            self.move.angular.z = z
            if blu_area and blu_area[0] > Driver.SLOW_DOWN_AREA_LOWER and blu_area[0] < Driver.SLOW_DOWN_AREA_UPPER:
                self.num_fast_frames = 0    
            else:
                self.num_fast_frames += 1
            self.acquire_lp = True
        else:
            self.acquire_lp = False

        if self.is_crossing_crosswalk:
            # print("crossing")
            self.crossing_crosswalk_count += 1
            x = round(self.move.linear.x, 4)
            z = round(self.move.angular.z, 4)
            if x > 0:
                x = Driver.CROSSWALK_X
            self.move.linear.x = x
            # if z != 0:
            #     self.move.angular.z = round(DataScraper.SET_Z*1.5,2)
            self.is_crossing_crosswalk = self.crossing_crosswalk_count < Driver.DRIVE_PAST_CROSSWALK_FRAMES  
            # red_area = PlatePull.get_contours_area(ImageProcessor.filter(cv_image, ImageProcessor.red_low, ImageProcessor.red_up))
            # print(red_area)
            # if not red_area:
            #     self.is_crossing_crosswalk = False
            # else:
            #     self.is_crossing_crosswalk = red_area[0] > Driver.CROSSWALK_BACK_AREA_THRES

        # check if red line close only when not crossing
        if not self.is_crossing_crosswalk and self.is_red_line_close(cv_image):
            self.crossing_crosswalk_count = 0 
            print("checking for red line")
            self.move.linear.x = 0.0
            self.move.angular.z = 0.0
            self.is_stopped_crosswalk = True
            self.first_stopped_frame = True

        pred_id, pred_id_vec = self.pr.prediction_data_id(cv_image)
        if pred_id:
            pred_lp, pred_lp_vecs = self.pr.prediction_data_license(cv_image)
            if pred_lp and self.acquire_lp:
                self.update_predictions(pred_id, pred_id_vec, pred_lp, pred_lp_vecs)

        try:
            # print("------------Move-------------",self.move.linear.x, self.move.angular.z)
            self.twist_pub.publish(self.move)
            pass
        except CvBridgeError as e: 
            print(e)

    def post_process_preds(self, min_prob=-1):
        for id in self.id_dict:
            self.id_stats_dict[id][1] = np.around(1.0*self.id_stats_dict[id][1] / self.id_stats_dict[id][0], 3)
        for k in self.lp_dict:
            val = [1.0*arr / self.lp_dict[k][0] for arr in self.lp_dict[k][1]]
            # print("K,VAL", k, val)
            val = [np.around(v,decimals=3) for v in val]
            # print("K,VAL rounded", k, val)
            self.lp_dict[k][1] = np.array(val)
            flg = False
            # self.lp_dict[k] = (self.lp_dict[k][0], val)
            # for p in self.lp_dict[k][1]:
            #     if np.max(p) < min_prob:
            #         flg = True
            #         break
            # if flg and self.id_dict[k]:
            #     print("DELETED:")
            #     print(k, self.lp_dict[k])
            #     del self.lp_dict[k]

    def update_predictions(self, pred_id, pred_id_vec, pred_lp, pred_lp_vecs):
        # id -> set[license plates]
        if not pred_id in self.id_dict:
            self.id_dict[pred_id] = set()
        self.id_dict[pred_id].add(pred_lp)

        # id -> (freq, prediction vector)
        if not pred_id in self.id_stats_dict:
            self.id_stats_dict[pred_id] = [1,pred_id_vec]
        else:
            self.id_stats_dict[pred_id][0] += 1
            self.id_stats_dict[pred_id][1] += pred_id_vec

        # license plates -> (freq, prediction vector)
        if not pred_lp in self.lp_dict:
            self.lp_dict[pred_lp] = [1, pred_lp_vecs]
        else:
            self.lp_dict[pred_lp][0] += 1
            self.lp_dict[pred_lp][1] += pred_lp_vecs
            # self.lp_dict[pred_lp] = (freq, p_v)

    # def get_pred_sum(self, pred_lp_vecs, id=True):
    #     # summed_pred_vec = []
    #     if id:
    #         sum = np.sum(pred_lp_vecs, axis=0)
    #         return sum
    #     else:
    #         sum = []
    #         pred_lp_vecs = np.array(pred_lp_vecs)
    #         print(pred_lp_vecs.shape)
    #         pred_lp_vecs = pred_lp_vecs[0]
    #         for i in range(0, 4):
    #             print(pred_lp_vecs[:,0])
    #             sum.append(np.sum(pred_lp_vecs[:,i], axis=0))
    #         return sum

    # def post_process_predictions(self, min_prob):
    #     for id in self.id_dict:
    #         # weighted sum
    #         summed = self.get_pred_sum(self.id_stats_dict[id][1])
    #         self.id_stats_dict[id][1] = summed / self.id_stats_dict[id][0]

    #     for k in self.lp_dict:
    #         summed = self.get_pred_sum(self.lp_dict[k][1], id=False)
    #         flg = False
    #         weighted_summed = self.lp_dict[k][0]*summed
    #         weighted_thres = min_prob*self.lp_dict[k][0]
    #         for p in weighted_summed:
    #             if np.max(p) < weighted_thres:
    #                 flg = True
    #                 break
    #         if flg:
    #             del self.lp_dict[k]
    #         else:
    #             self.lp_dict[k][1] = weighted_summed


    # def add_predictions(self, pred_id, pred_id_vec, pred_lp, pred_lp_vecs):
    #     # id -> set[license plates]
    #     if not pred_id in self.id_dict:
    #         self.id_dict[pred_id] = set()
    #     self.id_dict[pred_id].add(pred_lp)

    #     # id -> (freq, prediction vector)
    #     if not pred_id in self.id_stats_dict:
    #         self.id_stats_dict[pred_id] = [1,[pred_id_vec]]
    #     else:
    #         self.id_stats_dict[pred_id][0] += 1
    #         self.id_stats_dict[pred_id][1].append(pred_id_vec)

    #     # license plates -> (freq, prediction vector)
    #     if not pred_lp in self.lp_dict:
    #         self.lp_dict[pred_lp] = [1, [pred_lp_vecs]]
    #     else:
    #         self.lp_dict[pred_lp][0] += 1
    #         self.lp_dict[pred_lp][1].append(pred_lp_vecs)

    def is_straightened(self, img):
        """ Determines whether or not the robot is straightened to the red line

        Args:
            img (cv::Mat): Raw image data

        Returns:
            int: -2 if error state, -1 if currently to the left, 0 if straight within thres, 1 if currently to the right
        """        
        red_im = ImageProcessor.filter_red(img)
        edges = cv2.Canny(red_im,50,150,apertureSize = 3)
        minLineLength=100
        lines = cv2.HoughLinesP(image=edges,rho=1,theta=np.pi/180, threshold=100,lines=np.array([]), minLineLength=minLineLength,maxLineGap=80)
        if list(lines):
            x1,y1,x2,y2 = lines[0][0].tolist()
            deg = 0
            if x1 == x2:
                print("--- x1=x2 ---",x1,x2)
                return (-2,-2)
            deg = np.rad2deg(np.arctan((y2-y1)/(x2-x1)))
            print(deg)
            ang_state = 0
            lin_state = 0
            if abs(deg) < Driver.STRAIGHT_DEGS_THRES:
                ang_state = 0
            elif deg < 0:
                ang_state = -1
            else:
                ang_state = 1
            y = (y1+y2)/2.0
            y_tgt = Driver.RED_INTERSEC_PIX
            y_thres = Driver.RED_INTERSEC_PIX_THRES
            if y < y_tgt + y_thres and y > y_tgt - y_thres:
                lin_state = 0
            elif y < y_tgt:
                lin_state = -1
            else:
                lin_state = 1
            return (ang_state, lin_state)
        return (-2,-2)

    def print_stats(self):
        print("IDS:")
        for id in self.id_dict:
            print("----", id, "-----")
            print(self.id_dict[id])
            print(self.id_stats_dict[id])
            print("MAX: ", np.amax(self.id_stats_dict[id][1]))
        print("\n")
        print("LPS")
        for k in self.lp_dict:
            maxs = []
            print("----", k, "-----")
            print(self.lp_dict[k][0])
            for c in self.lp_dict[k][1]:
                maxs.append(np.amax(c))
            print("MAXS: ", maxs)

    def get_plate_results(self):
        combos = {}
        for id in self.id_dict:
            best_lp = None
            best_lp_freqs = 0
            for lp in self.id_dict[id]:
                # highest freqs
                if best_lp_freqs < self.lp_dict[lp][0]:
                    best_lp_freqs = self.lp_dict[lp][0]
                    best_lp = lp
                combos[id] = best_lp
        return combos

    def is_red_line_close(self, img):  
        """Determines whether or not the robot is close to the red line.

        Args:
            img (cv::Mat): The raw RGB image data to check if there is a red line

        Returns:
            bool: True if deemed close to the red line, False otherwise.
        """        
        red_filt = ImageProcessor.filter(img, ImageProcessor.red_low, ImageProcessor.red_up)
        # cv2.imshow('script_view', red_filt)
        # cv2.waitKey(3)
        area = PlatePull.get_contours_area(red_filt,2)
        # print("---Red Area", area)
        if not list(area):
            return False

        if len(list(area)) == 1:
            return area[0] > Driver.CROSSWALK_FRONT_AREA_THRES

        # return area[0] > Driver.CROSSWALK_FRONT_AREA_THRES and area[1] > Driver.CROSSWALK_BACK_AREA_THRES
        return area[0] > Driver.CROSSWALK_FRONT_AREA_THRES
    
    @staticmethod
    def has_red_line(img):
        red_filt = ImageProcessor.filter(img, ImageProcessor.red_low, ImageProcessor.red_up)
        return ImageProcessor.compare_frames(red_filt, np.zeros(red_filt.shape))

    def can_cross_crosswalk(self, img): 
        """Determines whether or not the robot can drive past the crosswalk. Only to be called when 
        it is stopped in front of the red line. 
        Updates this object.

        Can cross if the following conditions are met:
        - First the robot has stopped for a sufficient amount of time to account for stable field of view 
        due to inertia when braking
        - Robot must see the pedestrian move across the street at least once
        - Robot must see the pedestrian stopped at least once
        - Robot must see the pedestrian to be in a stopped state.

        Args:
            img (cv::Mat): Raw RGB iamge data

        Returns:
            bool: True if the robot able to cross crosswalk, False otherwise
        """        
        img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img_gray = ImageProcessor.crop(img_gray, 180, 720-180, 320, 1280-320)
        if self.prev_mse_frame is None:
            self.prev_mse_frame = img_gray
            return False
        
        
        mse = ImageProcessor.compare_frames(self.prev_mse_frame, img_gray)
        print("mse:", mse)
        print("first ped stopped, first ped move:" , self.first_ped_stopped, self.first_ped_moved)
        self.prev_mse_frame = img_gray
        
        if self.first_stopped_frames_count <= int(Driver.FIRST_STOP_SECS*Driver.FPS):
            self.first_stopped_frames_count += 1
            return False

        if mse < Driver.CROSSWALK_MSE_STOPPED_THRES:
            if not self.first_ped_stopped:
                self.first_ped_stopped = True
                return False
            if self.first_ped_moved and self.first_ped_stopped:
                self.prev_mse_frame = None
                self.first_stopped_frames_count = 0
                return True

        if mse > Driver.CROSSWALK_MSE_MOVING_THRES:
            if not self.first_ped_moved:
                self.first_ped_moved = True
                return False

        return False
    
    def start_seq(self):
        if (self.start_counter < 10):
            pass
        elif (self.start_counter == 10): 
            print(self.start_counter)
            self.license_pub.publish(String('TeamYoonifer,multi21,0,AA00'))
        elif (self.start_counter == 4000):  #arbitrary number
            self.license_pub.publish(String('TeamYoonifer,multi21,-1,AA00'))
        else:

            # if (self.start_counter < 30):
            #     self.move.linear.x = 0.35
            #     self.move.angular.z = 0.7
            if (self.start_counter < 20):
                self.move.linear.x = 0.7
                self.move.angular.z = 1.4
            # elif (self.start_counter < 42):
            #     self.move.linear.x = 0
            #     self.move.angular.z = 1.4
            elif (self.start_counter < 26):
                self.move.linear.x = 0
                self.move.angular.z = 2.8
            else:
                self.move.linear.x = 0
                self.move.angular.z = 0
                self.start_seq_state = False
            self.twist_pub.publish(self.move)
            print(self.start_counter)
            # cv2.waitKey(3)

        self.start_counter += 1

def main(args):    
    rospy.init_node('Driver', anonymous=True)
    dv = Driver()
    try:
        rospy.spin()
    except KeyboardInterrupt:
        ("Shutting down")
    cv2.destroyAllWindows()
    print("end")

if __name__ == '__main__':
    main(sys.argv)

