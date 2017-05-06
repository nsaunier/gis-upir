import time, logging
import math
import numpy
from scipy import linalg, spatial
import shapely.geometry as sg

from spat import markov, utility, facility, kalman
from spat.trajectory import features, model


def ellipse_bounds(state, quantile):
    ell = quantile * linalg.sqrtm(state.P[0:2, 0:2])
    height = math.sqrt(ell[0][0] ** 2 + ell[1][0] ** 2)
    width = math.sqrt(ell[0][1] ** 2 + ell[1][1] ** 2)
    return utility.bb_bounds(state.x[0], state.x[1], width, height)


def projection_distance_cost(state1, state2, travelled_distance):
    return state1.measurment_distance(state2.x[0] + travelled_distance, [1.0, 0.0], [state2.P[0, 0] + 2.0])


class Segment:
    def __init__(self, edge, coordinates, offset: float, width: float, distance: float, transition):
        self.distance = distance
        self.origin = numpy.asarray(coordinates[0])
        self.destination = numpy.asarray(coordinates[1])
        self.width = (width / (2.33*2.0))**2.0
        self.edge = edge

        v = self.destination - self.origin
        self.length = spatial.distance.euclidean(self.origin, self.destination)

        self.direction = v / self.length
        self.normal = utility.normal2d(v) / self.length

        def projection_along(v):
            return numpy.asmatrix([
                numpy.hstack((v, numpy.zeros(2))),
                numpy.hstack((numpy.zeros(2), v))])

        self.H = projection_along(self.normal)
        self.D = projection_along(self.direction)

        F, Q = transition
        self.F = self.D * F * self.D.T
        self.Q = self.D * Q * self.D.T
        self.normal_distance = numpy.dot(self.normal, self.origin) + offset
        self.direction_distance = numpy.dot(self.direction, self.origin)

    def project(self, state: kalman.KalmanFilter):
        if self.empty():
            return numpy.inf, None
        try:

            cost = state.measurment_update(
                [self.normal_distance, 0.0],
                self.H,
                numpy.diag([self.width, 1.0]))

            cost += state.ineq_constraint_update(
                self.D,
                [self.direction_distance, 0.0],
                [self.direction_distance + self.length, 50.0])

        except ValueError:
            return numpy.inf, None, None

        projected_state = state.transform(self.D)
        projected_state.x[0] -= self.direction_distance

        return cost, state, projected_state

    def advance(self, projected_state):
        projected_state.time_update(self.F, self.Q)
        return projected_state

    def empty(self):
        return self.length == 0.0


class Link:
    def __init__(self, graph: facility.SpatialGraph, transition, edge):
        self.segments = []
        self.length = 0.0
        for coord in utility.pairwise(graph.edge_geometry(*edge)):
            segment = Segment(edge, coord, 0.0, 2.0, self.length, transition)
            self.segments.append(segment)
            self.length += segment.length

    def __len__(self):
        return len(self.segments)

    def __getitem__(self, key) -> Segment:
        return self.segments[key]

    def at(self, idx) -> Segment:
        return self.segments[idx]


class LinkManager:
    def __init__(self, graph: facility.SpatialGraph, transition):
        self.graph = graph
        self.transition = transition
        self.link_table = {}

    def at(self, edge) -> Link:
        if edge not in self.link_table:
            self.link_table[edge] = Link(self.graph, self.transition, edge)
        return self.link_table[edge]


class ProjectionManager:
    def __init__(self, states, graph: facility.SpatialGraph, geometry: LinkManager):
        self.states = states
        self.graph = graph
        self.link_manager = geometry
        self.projection_table = {}
        self.state_table = {}
        self.edge_table = {}
        self.quantile = 25.0

        for i, state in enumerate(states):
            self.edge_table[i] = {}

    def project_state(self, i, quantile = 5.0):
        if i not in self.state_table:

            bounds = ellipse_bounds(self.states[i], quantile)
            projections = []
            projection_costs = []

            def visit_edge(i, edge):
                self.state_table[i][edge] = []
                link = self.link_manager.at(edge)
                for offset, segment in enumerate(utility.pairwise(self.graph.edge_geometry(u, v))):
                    if utility.intersect(sg.LineString(segment).bounds, bounds):
                        link = self.link_manager.at(edge)
                        cost, constrained_state, projected_state = link[offset].project(self.states[i].copy())
                        projections.append((edge, offset, constrained_state, projected_state))
                        projection_costs.append(cost)

            self.state_table[i] = {}
            for x in self.graph.search_edges(bounds):
                u, v = x.object
                visit_edge(i, (u, v))
                visit_edge(i, (v, u))

            k = min(5, len(projections))
            indices = numpy.argpartition(projection_costs, k-1)[0:k]
            for k in indices:
                edge, offset, constrained_state, projected_state = projections[k]
                cost = projection_costs[k]
                self.state_table[i][edge].append(offset)
                self.projection_table[i, edge, offset] = (cost, constrained_state, projected_state)

        return self.state_table[i]

    def search_edge(self, i, edge):
        if edge not in self.edge_table[i]:
            self.edge_table[i][edge] = []
            u, v = edge
            bounds = ellipse_bounds(self.states[i], self.quantile)
            for offset, segment in enumerate(utility.pairwise(self.graph.edge_geometry(u, v))):
                if utility.intersect(sg.LineString(segment).bounds, bounds):
                    self.edge_table[i][edge].append(offset)

        return self.edge_table[i][edge]

    def at(self, i, edge, offset):
        if (i, edge, offset) not in self.projection_table:
            link = self.link_manager.at(edge)
            self.projection_table[i, edge, offset] = link[offset].project(self.states[i].copy())

        return self.projection_table[i, edge, offset]


class LinkedNode:
    def __init__(self, edge, offset: int, idx: int, link: Link, cost: float,
                 constrained_state: kalman.KalmanFilter,
                 projected_state: kalman.KalmanFilter):
        self.edge = edge
        self.offset = offset
        self.idx = idx
        self.state_cost = cost
        self.constrained_state = constrained_state
        self.projected_state = projected_state

        self.link = link
        self.segment = self.link[self.offset]

        self.next_projected_state = self.segment.advance(self.projected_state.copy())

    def __str__(self):
        return "LinkedNode: " + str((self.edge, self.offset, self.idx))

    class Key:
        def __init__(self, edge, offset, idx):
            self.edge = edge
            self.offset = offset
            self.idx = idx

        def __eq__(self, other):
            return (isinstance(other, self.__class__) and
                    self.edge == other.edge and
                    self.offset == other.offset and
                    self.idx == other.idx)

        def __hash__(self):
            return hash((self.edge, self.offset, self.idx))

        def make_node(self, states, projections: ProjectionManager, geometry: LinkManager):
            return LinkedNode(self.edge, self.offset, self.idx, geometry.at(self.edge),
                              *projections.at(self.idx, self.edge, self.offset))

        def progress(self):
            return self.edge, self.idx

    def cost(self):
        return self.state_cost

    def coordinates(self):
        return self.constrained_state.x[0:2]

    def projection(self):
        return self.projected_state.x[0] + self.segment.distance

    def adjacent_nodes(self, states, projections: ProjectionManager, graph: facility.SpatialGraph, geometry: LinkManager):
        if self.idx + 1 == len(states):
            yield FinalNode()
            return

        for offset in projections.search_edge(self.idx + 1, self.edge):
            if offset >= self.offset:
                yield LinkedNode.Key(self.edge, offset, self.idx + 1)
        yield JumpingNode.Key(self)

        distance = self.link.length - self.segment.distance
        for next_edge in graph.adjacent(self.edge[1]):
            u, v = next_edge
            if (v, u) != self.edge:
                yield ForwardingNode.Key(self, distance, next_edge, self.next_projected_state)

    def distance_to(self, other):
        if isinstance(other, LinkedNode):
            assert self.edge == other.edge
            return abs(other.projection() - self.projection())  # difference on same edge
        elif isinstance(other, ForwardingNode):
            return self.link.length - (self.segment.distance + self.projected_state.x[0])  # remaining length of edge
        return 0.0

    def cost_to(self, other, distance_cost_fcn, intersection_cost_fcn):
        if isinstance(other, FinalNode):
            return 0.0
        cost = self.distance_to(other) * distance_cost_fcn(self.edge)
        if isinstance(other, LinkedNode):
            cost += projection_distance_cost(self.next_projected_state, other.projected_state,
                                             other.segment.distance - self.segment.distance)
        return cost

    def handicap(self, distance_cost_fcn):
        return 0.0

    def heuristic(self, states, distance_cost_fcn, cumulative_distance, greedy_factor):
        if self.idx + 1 < len(states):
            return greedy_factor * (spatial.distance.euclidean(self.coordinates(), states[self.idx + 1].x[0:2]) -
                                    cumulative_distance[self.idx + 1])
        else:
            return -greedy_factor * cumulative_distance[-1]


class ForwardingNode:
    def __init__(self, anchor: LinkedNode, distance: float, edge, link: Link, projected_state):
        self.anchor = anchor
        self.distance = distance
        self.projected_state = projected_state
        self.edge = edge
        self.link = link

    def __str__(self):
        return "ForwardingNode: " + str((self.anchor.edge, self.anchor.offset, self.anchor.idx,
                                         self.distance, self.edge))

    def __lt__(self, other):
        return self.edge < other.edge

    class Key:
        def __init__(self, anchor, distance, edge, projected_state):
            self.anchor = anchor
            self.distance = distance
            self.edge = edge
            self.projected_state = projected_state

        def __eq__(self, other):
            return (isinstance(other, self.__class__) and
                    self.anchor.idx == other.anchor.idx and
                    self.edge == other.edge)

        def __hash__(self):
            return hash((self.anchor.idx, self.edge))

        def make_node(self, states, projections: ProjectionManager, geometry: LinkManager):
            return ForwardingNode(self.anchor, self.distance, self.edge, geometry.at(self.edge), self.projected_state)

        def progress(self):
            return self.edge, self.anchor.idx

    def cost(self):
        return 0.0

    def coordinates(self):
        return self.link[-1].origin

    def adjacent_nodes(self, states, projections: ProjectionManager, graph: facility.SpatialGraph, geometry: LinkManager):
        for offset in projections.search_edge(self.anchor.idx + 1, self.edge):
            yield LinkedNode.Key(self.edge, offset, self.anchor.idx + 1)

        distance = self.distance + self.link.length
        for next_edge in graph.adjacent(self.edge[1]):
            u, v = next_edge
            if (v, u) != self.edge:
                yield ForwardingNode.Key(self.anchor, distance, next_edge, self.projected_state)

    def distance_to(self, other):
        if isinstance(other, LinkedNode):
            assert other.edge == self.edge # other is always on the same edge as self
            return other.segment.distance + other.projected_state.x[0]
        elif isinstance(other, ForwardingNode):
            return self.link.length  # other is always on a segment adjacent to self

    def cost_to(self, other, distance_cost_fcn, intersection_cost_fcn):
        cost = self.distance_to(other) * distance_cost_fcn(self.edge)
        if isinstance(other, LinkedNode):
            assert other.edge == self.edge
            segment = other.segment
            cost += projection_distance_cost(self.projected_state, other.projected_state, self.distance + segment.distance)
        return cost

    def handicap(self, distance_cost_fcn):
        return self.projected_state.ineql_constraint_distance([1.0, 0.0], self.distance)

    def heuristic(self, states, distance_cost_fcn, cumulative_distance, greedy_factor):
        return (greedy_factor * (spatial.distance.euclidean(self.coordinates(), states[self.anchor.idx + 1].x[0:2]) -
                                 cumulative_distance[self.anchor.idx + 1]))


class FloatingNode:
    def __init__(self, idx, state: kalman.KalmanFilter):
        self.idx = idx
        self.state = state

    def __str__(self):
        return "FloatingNode: " + str(self.idx)

    class Key:
        def __init__(self, idx: int):
            self.idx = idx

        def __eq__(self, other):
            return (isinstance(other, self.__class__) and
                    self.idx == other.idx)

        def __hash__(self):
            return hash(self.idx)

        def make_node(self, states, projections: ProjectionManager, geometry: LinkManager):
            return FloatingNode(self.idx, states[self.idx])

        def progress(self):
            return None, self.idx

    def cost(self):
        return 20.0

    def coordinates(self):
        return self.state.x[0:2]

    def adjacent_nodes(self, states, projections: ProjectionManager, graph: facility.SpatialGraph, geometry: LinkManager):
        if self.idx + 1 == len(states):
            yield FinalNode()
            return
        yield FloatingNode.Key(self.idx + 1)
        for edge, offsets in projections.project_state(self.idx+1).items():
            link = geometry.at(edge)
            for offset in offsets:
                yield LinkedNode.Key(edge, offset, self.idx+1)

    def distance_to(self, other) -> float:
        # Works for FloatingNode and LinkedNode
        return spatial.distance.euclidean(self.coordinates(), other.coordinates())

    def cost_to(self, other, distance_cost_fcn, intersection_cost_fcn):
        if isinstance(other, FinalNode):
            return 0.0
        return self.distance_to(other) * distance_cost_fcn(None)

    def handicap(self, distance_cost_fcn):
        return 0.0

    def heuristic(self, states, distance_cost_fcn, cumulative_distance, greedy_factor):
        return -greedy_factor * cumulative_distance[self.idx]


class JumpingNode:
    def __init__(self, anchor: LinkedNode, state: kalman.KalmanFilter):
        self.anchor = anchor
        self.state = state

    def __str__(self):
        return "JumpingNode: " + str(self.anchor.idx)

    class Key:
        def __init__(self, anchor: LinkedNode):
            self.anchor = anchor

        def __eq__(self, other):
            return (isinstance(other, self.__class__) and
                    self.anchor.idx == other.anchor.idx)

        def __hash__(self):
            return hash(self.anchor.idx)

        def make_node(self, states, projections: ProjectionManager, geometry: LinkManager):
            return JumpingNode(self.anchor, states[self.anchor.idx+1])

        def progress(self):
            return None, self.anchor.idx

    def cost(self):
        return 20.0

    def coordinates(self):
        return self.state.x[0:2]

    def adjacent_nodes(self, states, projections: ProjectionManager, graph: facility.SpatialGraph, geometry: LinkManager):
        yield FloatingNode.Key(self.anchor.idx + 1)

        for edge, offsets in projections.project_state(self.anchor.idx+1).items():
            u, v = edge
            if (u, v) == self.anchor.edge or (v, u) == self.anchor.edge:
                continue
            for offset in offsets:
                yield LinkedNode.Key(edge, offset, self.anchor.idx+1)

    def distance_to(self, other) -> float:
        # Works for FloatingNode and LinkedNode
        return spatial.distance.euclidean(self.anchor.coordinates(), other.coordinates())

    def cost_to(self, other, distance_cost_fcn, intersection_cost_fcn):
        return self.distance_to(other) * distance_cost_fcn(None)

    def handicap(self, distance_cost_fcn):
        return self.distance_to(self) * distance_cost_fcn(None)

    def heuristic(self, states, distance_cost_fcn, cumulative_distance, greedy_factor):
        return - greedy_factor * cumulative_distance[self.anchor.idx+1]


class InitialNode:

    def __eq__(self, other):
        return isinstance(other, self.__class__)

    def __hash__(self):
        return hash(0)

    def __str__(self):
        return "InitialNode"

    def make_node(self, states, projections: ProjectionManager, geometry: LinkManager):
        return self

    def cost(self):
        return 0.0

    def progress(self):
        return None, 0

    @staticmethod
    def adjacent_nodes(states, projections: ProjectionManager, graph: facility.SpatialGraph, geometry: LinkManager):
        for edge, offsets in projections.project_state(0, 50.0).items():
            link = geometry.at(edge)
            for offset in offsets:
                yield LinkedNode.Key(edge, offset, 0)

    @staticmethod
    def cost_to(other, distance_cost_fcn, intersection_cost_fcn):
        return 0.0

    def handicap(self, distance_cost_fcn):
        return 0.0

    @staticmethod
    def heuristic(states, distance_cost_fcn, cumulative_distance, greedy_factor):
        return 0.0


class FinalNode:
    def __eq__(self, other):
        return isinstance(other, self.__class__)

    def __hash__(self):
        return hash(0)

    def __str__(self):
        return "FinalNode"

    def make_node(self, states, projections: ProjectionManager, geometry: LinkManager):
        return self

    def cost(self):
        return 0.0

    def progress(self):
        return None, math.inf

    def handicap(self, distance_cost_fcn):
        return 0.0

    @staticmethod
    def heuristic(states, distance_cost_fcn, cumulative_distance, greedy_factor):
        return -greedy_factor * cumulative_distance[-1]


def format_path(path):
    geometry = []
    current_edge = None
    begin_bound = model.MatchedSegment.Bound(None, False, 0)
    previous_node = None

    for key, node in path:
        if current_edge is None and isinstance(node, LinkedNode):
            if geometry:
                assert previous_node is not None
                end_bound = model.MatchedSegment.Bound(None, False, node.idx)
                geometry.append(node.coordinates())
                yield model.MatchedSegment(None, geometry, begin_bound, end_bound)

            current_edge = node.edge
            begin_bound = model.MatchedSegment.Bound(node.projection(), False, node.idx)
            geometry = []

        if isinstance(node, ForwardingNode):
            end_bound = model.MatchedSegment.Bound(node.anchor.link.length, True, node.anchor.idx + 1)

            assert begin_bound is not None and current_edge is not None
            geometry.append(node.coordinates())
            yield model.MatchedSegment(current_edge, geometry, begin_bound, end_bound)

            current_edge = node.edge
            begin_bound = model.MatchedSegment.Bound(0.0, True, node.anchor.idx + 1)
            geometry = [node.coordinates()]

        if isinstance(node, JumpingNode):
            end_bound = model.MatchedSegment.Bound(node.anchor.projection(), False, node.anchor.idx + 1)
            yield model.MatchedSegment(current_edge, geometry, begin_bound, end_bound)

            current_edge = None
            begin_bound = model.MatchedSegment.Bound(None, False, node.anchor.idx + 1)
            geometry = [node.anchor.coordinates()]

        if isinstance(node, FinalNode):
            if current_edge is None:
                assert isinstance(previous_node, FloatingNode)
                end_bound = model.MatchedSegment.Bound(None, False, previous_node.idx + 1)
                if geometry:
                    yield model.MatchedSegment(None, geometry, begin_bound, end_bound)
            else:
                assert isinstance(previous_node, LinkedNode)
                end_bound = model.MatchedSegment.Bound(previous_node.projection(), False, previous_node.idx + 1)
                yield model.MatchedSegment(current_edge, geometry, begin_bound, end_bound)

        if isinstance(node, LinkedNode) or isinstance(node, FloatingNode):
            geometry.append(node.coordinates())

        previous_node = node


def solve_one(trajectory, graph, distance_cost_fcn, intersection_cost_fcn, greedy_factor):
    logging.info("solving mapmatch for %s", trajectory['id'])

    states = trajectory['state']
    link_manager = LinkManager(graph, trajectory['transition'])

    cumulative_distance = [0.0]
    total_distance = 0.0
    for s1, s2 in utility.pairwise(states):
        distance = spatial.distance.euclidean(s1.x[0:2], s2.x[0:2])
        total_distance += distance
        cumulative_distance.append(total_distance)

    projections = ProjectionManager(states, graph, link_manager)

    def adjacent_nodes(key):
        return key.adjacent_nodes(states, projections, graph, link_manager)

    def state_cost(key):
        return key.cost()

    def transition_cost(current_state, next_state):
        return current_state.cost_to(next_state, distance_cost_fcn, intersection_cost_fcn)

    def handicap(state):
        return state.handicap(distance_cost_fcn)

    def heuristic(state):
        return state.heuristic(states, distance_cost_fcn, cumulative_distance, greedy_factor)

    def progress(state):
        return state.progress()

    def project(key):
        return key.make_node(states, projections, link_manager)

    chain = markov.MarkovGraph(adjacent_nodes, project, state_cost, transition_cost, handicap)

    start_time = time.time()
    path = chain.find_best(InitialNode(), FinalNode(), progress, heuristic, 300000.0)
    logging.debug("elapsed_time: %.4f", time.time() - start_time)
    return path


def solve(trajectories, graph, greedy_factor):
    distance_weights = numpy.array([
        1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    intersection_weights = numpy.array([
        1.0, 0.0])

    def distance_cost(link):
        if link is None:
            return 300.0
        predicates = [
            lambda link: True,
            features.link_type_predicate(graph, features.any_cycling_link),
            features.link_type_predicate(graph, features.designated_roadway),
            features.link_type_predicate(graph, features.bike_lane),
            features.link_type_predicate(graph, features.seperate_cycling_link),
            features.link_type_predicate(graph, features.offroad_link),
            features.link_type_predicate(graph, features.other_road_type),
        ]
        return 0.5 * numpy.dot(
            numpy.array(list(map(lambda pred: pred(link), predicates))),
            distance_weights)

    end_of_facility = features.match_intersections(
        features.load_discontinuity("data/discontinuity/end_of_facility"), graph)

    def intersection_cost(u, v, k):
        node_predicates = [
            lambda link: True,
            features.intersection_collection(end_of_facility),
        ]
        return 0.5 * numpy.dot(
            numpy.array(map(lambda pred: pred(v), node_predicates)),
            intersection_weights)

    for trajectory in trajectories:
        path = solve_one(trajectory, graph, distance_cost, intersection_cost, greedy_factor)
        if path is None:
            logging.warning("trashing %s due to incomplete mapmatch", trajectory['id'])
            continue

        yield {
            'segment': list(format_path(path)),
            'id': trajectory['id'],
            'count': len(trajectory['state'])}