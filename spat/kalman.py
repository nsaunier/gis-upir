import math
import numpy as np
from scipy import linalg


def xcov(X, Y):
    n = X.shape[0]
    m = Y.shape[0]
    return np.cov(X, Y, bias=True)[0:n,-m:]


def logerfc(x):
    """ Computes log of erfc"""

    t = 1.0 / (1.0 + 0.5 * x)
    # use Horner's method
    return math.log(t) - x*x + (-1.26551223 +
                                t * ( 1.00002368 +
                                      t * ( 0.37409196 +
                                            t * ( 0.09678418 +
                                                  t * (-0.18628806 +
                                                       t * ( 0.27886807 +
                                                             t * (-1.13520398 +
                                                                  t * ( 1.48851587 +
                                                                        t * (-0.82215223 +
                                                                             t * ( 0.17087277))))))))))


def left_truncate_gaussian(a):
    """Truncates gaussian distribution and returns new statistics. 
  
    Given x ~ N(0, 1), returns statistics of x subject to a < x.
    
    Args:
      a: left bound
  
    Returns:
      log of probability
      updated mean 
      updated variance
    """
    if a > 0.0:
        l = logerfc(a / math.sqrt(2)) - math.log(2)
    else:
        l = math.log(math.erfc(a / math.sqrt(2)) / 2)
    alpha = math.sqrt(2) / (2 * math.sqrt(math.pi))
    c = math.exp(-(a*a) / 2 - l)

    u = alpha * c
    var = alpha * c * (a - 2*u) + u*u + 1.0
    return l, u, var


def right_truncate_gaussian(a):
    """Truncates gaussian distribution and returns new statistics. 
  
    Given x ~ N(0, 1), returns statistics of x subject to x < a.
    
    Args:
      a: right bound
  
    Returns:
      log of probability
      updated mean
      updated variance
    """
    if a < 0.0:
        l = logerfc(-a / math.sqrt(2)) - math.log(2)
    else:
        l = math.log(math.erfc(-a / math.sqrt(2)) / 2)
    alpha = -math.sqrt(2) / (2 * math.sqrt(math.pi))
    c = math.exp(-(a*a) / 2 - l)

    u = alpha * c
    var = alpha * c * (a - 2*u) + u*u + 1.0
    return l, u, var


def truncate_gaussian(a, b):
    """Truncates gaussian distribution and returns new statistics. 
  
    Given x ~ N(0, 1), returns statistics of x subject to a < x < b.
    
    Args:
      a: left bound
      b: right bound
  
    Returns:
      log of probability
      updated mean 
      updated variance
    """
    if a == -np.inf:
        return right_truncate_gaussian(b)
    elif b == np.inf:
        return left_truncate_gaussian(a)
    elif np.sign(a) != np.sign(b):
        sign = 1.0
        p = (math.erf(b / math.sqrt(2)) - math.erf(a / math.sqrt(2))) / 2
        l = np.log(p)
    else:
        sign = np.sign(a)
        if sign < 0:
            a, b = -b, -a
        e = logerfc(a / math.sqrt(2))
        f = logerfc(b / math.sqrt(2))
        l = e + math.log(1.0 - math.exp(f-e)) - math.log(2)

    alpha = math.sqrt(2) / (2 * math.sqrt(math.pi))
    c = math.exp(-(a*a)/2 - l)
    d = math.exp(-(b*b)/2 - l)

    u = alpha * (c - d)
    var = alpha * (c * (a - 2*u) - d * (b - 2*u)) + u*u + 1.0
    return l, u * sign, var


class KalmanFilter:
    def __init__(self, initial_state, initial_state_covariance):
        self.x = np.array(initial_state)
        self.P = np.matrix(initial_state_covariance)

    def copy(self):
        return KalmanFilter(self.x, self.P)

    def time_update(self, F, Q):
        self.x = np.dot(np.asarray(F), self.x)
        self.P = F * self.P * F.T + Q

    def unscented_time_update(self, f, Q):
        n = self.x.shape[0]
        noise = linalg.sqrtm(n * self.P).T
        sigmax = np.empty([2*n, n])
        for i in range(n):
            sigmax[i] = f(self.x + noise[i])
            sigmax[n+i] = f(self.x - noise[i])

        self.x = np.mean(sigmax, 0)
        self.P = np.cov(sigmax.T, bias=True) + Q

    def measurment_update(self, y, H, R):
        H = np.asmatrix(H)
        U = self.P * H.T
        S = np.linalg.inv(H * U + R)
        z = y - np.dot(np.asarray(H), self.x)
        distance = np.dot(np.dot(np.asarray(S), z), z)
        K = np.dot(U, S)
        self.x += np.dot(np.asarray(K), z)
        self.P -= K * H * self.P
        return distance / 2

    def unscented_measurment_update(self, y, h, R):
        y = np.asarray(y)
        n = self.x.shape[0]
        m = y.shape[0]

        noise = linalg.sqrtm(n * np.asarray(self.P)).T
        sigmax = np.empty([2*n, n])
        sigmay = np.empty([2*n, m])
        for i in range(n):
            sigmax[i] = self.x + noise[i]
            sigmax[n+i] = self.x - noise[i]
            sigmay[i] = h(sigmax[i])
            sigmay[n+i] = h(sigmax[n+i])
        Py = np.asmatrix(np.cov(sigmay.T, bias=True) + R)
        S = np.linalg.inv(Py)
        Pxy = xcov(sigmax.T, sigmay.T)
        z = y - np.mean(sigmay, 0)
        K = Pxy * S
        distance = np.dot(np.dot(np.asarray(S), z), z)
        self.x += np.dot(np.asarray(K), z)
        self.P = self.P - K * Py * K.T
        return distance / 2

    def smooth_update(self, next, F, Q):
        x1 = np.dot(F, self.x)
        P1 = F * self.P * F.T + Q
        K = self.P * F.T * np.linalg.inv(P1)
        self.x = self.x + np.dot(np.asarray(K), next.x - x1)
        self.P -= K * (P1 - next.P) * K.T

    def constraint_update(self, d, D):
        D = np.asmatrix(D)
        U = self.P * D.T
        z = d - np.dot(np.asarray(D), self.x)
        S = np.linalg.inv(D * U)
        distance = np.dot(z, np.dot(np.asarray(S), z))
        K = U * S
        self.x += np.dot(np.asarray(K), z)
        self.P = self.P - K * D * self.P
        return distance / 2

    def ineq_constraint_update(self, D, a, b):
        D = np.asarray(D)
        n = D.shape[0]
        distance = 0.0
        for i in range(0, n):
            omega = D[i,:]
            vv = np.dot(np.dot(omega, np.asarray(self.P)), omega)
            v = math.sqrt(vv)
            c = (a[i] - np.dot(omega, self.x)) / v
            d = (b[i] - np.dot(omega, self.x)) / v
            l, u, var = truncate_gaussian(c, d)
            distance -= l

            self.x += np.dot(np.asarray(self.P), omega * u) / v
            S = self.P * np.outer(omega, omega) * self.P / vv
            self.P += var * S - S
        return distance

    def transform(self, D):
        D = np.asmatrix(D)
        return KalmanFilter(np.dot(np.asarray(D), self.x), D * self.P * D.T)

    def measurment_distance(self, y, H, R):
        H = np.asmatrix(H)
        U = self.P * H.T
        S = np.linalg.inv(H * U + R)
        z = y - np.dot(np.asarray(H), self.x)
        distance = np.dot(np.dot(np.asarray(S), z), z)
        return distance / 2

    def eq_constraint_distance(self, d, D):
        D = np.asmatrix(D)
        S = np.linalg.inv(D * self.P * D.T)
        z = d - np.dot(np.asarray(D), self.x)
        distance = np.dot(z, np.dot(np.asarray(S), z))
        return distance / 2

    def ineq_constraint_distance(self, omega, a, b):
        vv = np.dot(np.dot(omega, np.asarray(self.P)), omega)
        if vv < 0:
            return np.inf
        v = math.sqrt(vv)
        c = (a - np.dot(omega, self.x)) / v
        d = (b - np.dot(omega, self.x)) / v
        l, u, var = truncate_gaussian(c, d)
        return -l

    def ineql_constraint_distance(self, omega, a):
        vv = np.dot(np.dot(omega, np.asarray(self.P)), omega)
        if vv < 0:
            return np.inf
        c = (a - np.dot(omega, self.x)) / math.sqrt(vv)
        l, u, var = left_truncate_gaussian(c)
        return -l

    def ineqr_constraint_distance(self, omega, b):
        vv = np.dot(np.dot(omega, np.asarray(self.P)), omega)
        if vv < 0:
            return np.inf
        d = (b - np.dot(omega, self.x)) / math.sqrt(vv)
        l, u, var = right_truncate_gaussian(d)
        return -l



