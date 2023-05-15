from pydrake.all import (AbstractValue, InputPortIndex, LeafSystem,
                         PiecewisePolynomial, PiecewisePose, RigidTransform,
                         RollPitchYaw, Sphere, Rgba)
from manipulation.meshcat_utils import AddMeshcatTriad
from manipulation.pick import (MakeGripperCommandTrajectory, MakeGripperPoseTrajectory)
from copy import copy

import numpy as np

import planner_state
import pick

SHELF_1 = (0, 0)

class Planner(LeafSystem):
    def __init__(self, plant, joint_count, meshcat, rs, prepick_distance):
        LeafSystem.__init__(self)
        self._gripper_body_index = plant.GetBodyByName("body").index()
        self.DeclareAbstractInputPort(
            "body_poses", AbstractValue.Make([RigidTransform()]))
        self._x_bin_grasp_index = self.DeclareAbstractInputPort(
            "x_bin_grasp", AbstractValue.Make(
                (np.inf, RigidTransform()))).get_index()
        self._wsg_state_index = self.DeclareVectorInputPort("wsg_state", 2).get_index()

        self._mode_index = self.DeclareAbstractState(
            AbstractValue.Make(planner_state.PlannerState.WAIT_FOR_OBJECTS_TO_SETTLE))
        self._traj_X_G_index = self.DeclareAbstractState(
            AbstractValue.Make(PiecewisePose()))
        self._traj_wsg_index = self.DeclareAbstractState(
            AbstractValue.Make(PiecewisePolynomial()))
        self._times_index = self.DeclareAbstractState(AbstractValue.Make(
            {"initial": 0.0}))
        self._attempts_index = self.DeclareDiscreteState(1)

        self.DeclareAbstractOutputPort(
            "X_WG", lambda: AbstractValue.Make(RigidTransform()),
            self.CalcGripperPose)
        self.DeclareVectorOutputPort("wsg_position", 1, self.CalcWsgPosition)
        #self.DeclareVectorOutputPort("base_position", 2, self.CalcBasePosition)

        # For GoHome mode.
        num_positions = joint_count
        self._iiwa_position_index = self.DeclareVectorInputPort(
            "iiwa_position", num_positions).get_index()
        self.DeclareAbstractOutputPort(
            "control_mode", lambda: AbstractValue.Make(InputPortIndex(0)),
            self.CalcControlMode)
        self.DeclareAbstractOutputPort(
            "reset_diff_ik", lambda: AbstractValue.Make(False),
            self.CalcDiffIKReset)
        self._q0_index = self.DeclareDiscreteState(num_positions)  # for q0
        self._traj_q_index = self.DeclareAbstractState(
            AbstractValue.Make(PiecewisePolynomial()))
        self.DeclareVectorOutputPort("iiwa_position_command", num_positions,
                                     self.CalcIiwaPosition)
        self.DeclareInitializationDiscreteUpdateEvent(self.Initialize)

        self._simulation_done = False
        self.DeclarePeriodicUnrestrictedUpdateEvent(0.1, 0.0, self.Update)
        
        self.meshcat = meshcat
        self.rs = rs
        self.prepick_distance = prepick_distance

    def Update(self, context, state):
        if self._simulation_done:
            return

        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        current_time = context.get_time()
        times = context.get_abstract_state(int(
            self._times_index)).get_value()

        if mode == planner_state.PlannerState.WAIT_FOR_OBJECTS_TO_SETTLE:
            if context.get_time() - times["initial"] > 1.0:
                self.Plan(context, state)
            else:
                self.GoToShelf(context, state)
            return          
        elif mode == planner_state.PlannerState.GO_HOME or mode == planner_state.PlannerState.GO_TO_SHELF:
            traj_q = context.get_mutable_abstract_state(int(
                self._traj_q_index)).get_value()
            if not traj_q.is_time_in_range(current_time):
                self.Plan(context, state)
            return

        #if abs(current_time - times["place_end"]) < 0.1:
        #    print(self.get_input_port(self._iiwa_position_index).Eval(context))

        # If we are between pick and place and the gripper is closed, then
        # we've missed or dropped the object.  Time to replan.
        if (current_time > times["postpick"] and
                current_time < times["preplace"]):
            wsg_state = self.get_input_port(self._wsg_state_index).Eval(context)
            if wsg_state[0] < 0.01:
                attempts = state.get_mutable_discrete_state(
                    int(self._attempts_index)).get_mutable_value()
                
                # exit
                if attempts[0] > 5:
                    attempts[0] = 0
                    return
                
                attempts[0] += 1
                self.GoHome(context, state)
                # state.get_mutable_abstract_state(int(
                #     self._mode_index)).set_value(
                #         planner_state.PlannerState.WAIT_FOR_OBJECTS_TO_SETTLE)
                # times = {"initial": current_time}
                # state.get_mutable_abstract_state(int(
                #     self._times_index)).set_value(times)
                # X_G = self.get_input_port(0).Eval(context)[int(
                #     self._gripper_body_index)]
                # state.get_mutable_abstract_state(int(
                #     self._traj_X_G_index)).set_value(PiecewisePose.MakeLinear([current_time, np.inf], [X_G, X_G]))
                return
            else:
                #self.PlaceItem(context, state)
                #self.FreezeBase(context)
                pass

        traj_X_G = context.get_abstract_state(int(
            self._traj_X_G_index)).get_value()
        if not traj_X_G.is_time_in_range(current_time):
            #self.Plan(context, state)
            self.GoHome(context, state)
            return

        X_G = self.get_input_port(0).Eval(context)[int(self._gripper_body_index)]

        # stop and replan
        if np.linalg.norm(traj_X_G.GetPose(current_time).translation()
                - X_G.translation()) > 0.2:
            self.GoHome(context, state)
            return

    def GoHome(self, context, state):
        print("Replanning due to large tracking error (go home).")
        state.get_mutable_abstract_state(int(
            self._mode_index)).set_value(
                planner_state.PlannerState.GO_HOME)
        q = self.get_input_port(self._iiwa_position_index).Eval(context)
        q0 = copy(context.get_discrete_state(self._q0_index).get_value())
        q0[:2] = q[:2]  # Safer to not reset the first joint.

        current_time = context.get_time()
        q_traj = PiecewisePolynomial.FirstOrderHold(
            [current_time, current_time + 5.0], np.vstack((q, q0)).T)
        state.get_mutable_abstract_state(int(
            self._traj_q_index)).set_value(q_traj)


    def GoToShelf(self, context, state):
        print("Going to shelf.")
        state.get_mutable_abstract_state(int(
            self._mode_index)).set_value(
                planner_state.PlannerState.GO_HOME)
        q = self.get_input_port(self._iiwa_position_index).Eval(context)
        q0 = copy(q)
        q0[:2] = SHELF_1
        current_time = context.get_time()
        q_traj = PiecewisePolynomial.FirstOrderHold(
            [current_time, current_time + 5.0], np.vstack((q, q0)).T)
        state.get_mutable_abstract_state(int(
            self._traj_q_index)).set_value(q_traj)

    # def PlaceItem(self, context, state):
    #     print("Placing")
    #     state.get_mutable_abstract_state(int(
    #         self._mode_index)).set_value(
    #             planner_state.PlannerState.GO_HOME)
    #     q_current = self.get_input_port(self._iiwa_position_index).Eval(context)
    #     q_clearance = copy(context.get_discrete_state(self._q0_index).get_value())
    #     q_clearance[:3] = (q_current[0], q_current[1] + 0.1, 0)
    #     q_place = copy(q_clearance)
    #     q_place[3] = 1
    #     q_place[5] = -1.2
    #     q_place[7] = 0.6


    #     current_time = context.get_time()
    #     q_traj = PiecewisePolynomial.FirstOrderHold(
    #         [current_time, current_time + 2.0, current_time + 4.0], np.vstack((q_current, q_clearance, q_place)).T)
    #     state.get_mutable_abstract_state(int(
    #         self._traj_q_index)).set_value(q_traj)

    # def FreezeBase(self, context):
    #     params = self.diff_ik.get_mutable_parameters()

    #     q = self.get_input_port(self._iiwa_position_index).Eval(context)
    #     position_limits = params.get_joint_position_limits()
    #     position_limits[0][:2] = q[:2]
    #     position_limits[1][:2] = q[:2]
    #     params.set_joint_position_limits(position_limits)

    #     velocity_limits = params.get_joint_velocity_limits()
    #     velocity_limits[0][:2] = 0
    #     velocity_limits[1][:2] = 0
    #     params.set_joint_velocity_limits(velocity_limits)

    def Plan(self, context, state):
        mode = copy(
            state.get_mutable_abstract_state(int(self._mode_index)).get_value())

        X_G = {
            "initial":
                self.get_input_port(0).Eval(context)
                [int(self._gripper_body_index)]
        }

        # pick pose calculation
        cost = np.inf
        mode = planner_state.PlannerState.PICKING_FROM_SHELF_1
        for i in range(5):
            cost, X_G["pick"] = self.get_input_port(
                self._x_bin_grasp_index).Eval(context)
            
            #X_G["pick"] = X_G["pick"] @ RigidTransform(RotationMatrix.MakeXRotation(-np.pi / 2))
            
            if not np.isinf(cost):
                break
        
        if np.isinf(cost):
            self._simulation_done = True
            print("Could not find a valid grasp in either bin after 5 attempts")
            return
        state.get_mutable_abstract_state(int(self._mode_index)).set_value(mode)

        #self.FreezeBase(context)

        # place pose calculation
        if mode == planner_state.PlannerState.PICKING_FROM_SHELF_1:
            x_range = np.array([.35, .6])
            y_range = np.array([-.1, .1])

            x_range += SHELF_1[0]
            y_range += SHELF_1[1]

            # Place in Y bin:
            X_G["place"] = RigidTransform(
                RollPitchYaw(-np.pi / 2, 0, np.pi / 2),
                [self.rs.uniform(x_range[0], x_range[1]),
                 self.rs.uniform(y_range[0], y_range[1]),
                 0.2])
                
            self.meshcat.SetObject("place1", Sphere(0.02), rgba=Rgba(.9, .1, .1, 1))
            self.meshcat.SetTransform("place1", RigidTransform([x_range[0], y_range[0], 0]))
            self.meshcat.SetObject("place2", Sphere(0.02), rgba=Rgba(.1, .9, .1, 1))
            self.meshcat.SetTransform("place2", RigidTransform([x_range[1], y_range[1], 0]))

        # plan trajectory
        X_G, times = pick.MakeGripperFrames(X_G, t0=context.get_time(), prepick_distance=self.prepick_distance)
        print(
            f"t={int(context.get_time())}s - Planned {int(times['postplace'] - times['initial'])} seconds trajectory for picking from the shelf."
        )
        state.get_mutable_abstract_state(int(
            self._times_index)).set_value(times)


        if True:  # Useful for debugging
            AddMeshcatTriad(self.meshcat, "X_Ginitial", X_PT=X_G["initial"])
            #AddMeshcatTriad(self.meshcat, "X_Gclearance", X_PT=X_G["clearance"])
            AddMeshcatTriad(self.meshcat, "X_Gprepick", X_PT=X_G["prepick"])
            AddMeshcatTriad(self.meshcat, "X_Gpick", X_PT=X_G["pick"])
            AddMeshcatTriad(self.meshcat, "X_Gpreplace", X_PT=X_G["preplace"])
            AddMeshcatTriad(self.meshcat, "X_Gplace", X_PT=X_G["place"])

        traj_X_G = pick.MakeGripperPoseTrajectory(X_G, times)
        traj_wsg_command = MakeGripperCommandTrajectory(times)

        state.get_mutable_abstract_state(int(
            self._traj_X_G_index)).set_value(traj_X_G)
        state.get_mutable_abstract_state(int(
            self._traj_wsg_index)).set_value(traj_wsg_command)

    def start_time(self, context):
        return context.get_abstract_state(
            int(self._traj_X_G_index)).get_value().start_time()

    def end_time(self, context):
        return context.get_abstract_state(
            int(self._traj_X_G_index)).get_value().end_time()

    def CalcGripperPose(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        traj_X_G = context.get_abstract_state(int(
            self._traj_X_G_index)).get_value()
        if (traj_X_G.get_number_of_segments() > 0 and
                traj_X_G.is_time_in_range(context.get_time())):
            # Evaluate the trajectory at the current time, and write it to the
            # output port.
            output.set_value(
                context.get_abstract_state(int(
                    self._traj_X_G_index)).get_value().GetPose(
                        context.get_time()))
            return

        # Command the current position (note: this is not particularly good if the velocity is non-zero)
        output.set_value(self.get_input_port(0).Eval(context)
            [int(self._gripper_body_index)])

    def CalcWsgPosition(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()
        opened = np.array([0.107])
        closed = np.array([0.0])

        if mode == planner_state.PlannerState.GO_HOME:
            # Command the open position
            output.SetFromVector([opened])
            return

        traj_wsg = context.get_abstract_state(int(
            self._traj_wsg_index)).get_value()
        if (traj_wsg.get_number_of_segments() > 0 and
                traj_wsg.is_time_in_range(context.get_time())):
            # Evaluate the trajectory at the current time, and write it to the
            # output port.
            output.SetFromVector(traj_wsg.value(context.get_time()))
            return

        # Command the open position
        output.SetFromVector([opened])

    def CalcBasePosition(self, context, output):
        output.SetFromVector([0, 0])

    def CalcControlMode(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        if mode == planner_state.PlannerState.GO_HOME or mode == planner_state.PlannerState.GO_TO_SHELF:
            output.set_value(InputPortIndex(2))  # Go Home
        else:
            output.set_value(InputPortIndex(1))  # Diff IK

    def CalcDiffIKReset(self, context, output):
        mode = context.get_abstract_state(int(self._mode_index)).get_value()

        if mode == planner_state.PlannerState.GO_HOME or mode == planner_state.PlannerState.GO_TO_SHELF:
            output.set_value(True)
        else:
            output.set_value(False)

    def Initialize(self, context, discrete_state):
        discrete_state.set_value(
            int(self._q0_index),
            self.get_input_port(int(self._iiwa_position_index)).Eval(context))

    def CalcIiwaPosition(self, context, output):
        traj_q = context.get_mutable_abstract_state(int(
                self._traj_q_index)).get_value()

        output.SetFromVector(traj_q.value(context.get_time()))