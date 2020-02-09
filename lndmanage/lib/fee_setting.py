import logging
import time

from lndmanage.lib.user import yes_no_question
from lndmanage.lib.forwardings import ForwardingAnalyzer

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# fee_lock_rate is used to disable forwardings of a channel via fees
FEE_LOCK_RATE = 0.025000
# threshold_ub_open is the value of unbalancedness below which the channel goes
# into full open mode if no forwardings where happening
THRESHOLD_UB_OPEN = -0.5
# threshold_ub_close is the value of unbalancedness above which the channel
# goes into closed mode
THRESHOLD_UB_CLOSE = 0.95


class FeeSetter(object):
    """
    Class for setting fees.
    """
    def __init__(self, node):
        """
            :param node: node instance
            :type node: `class`:lib.node.Node
        """
        self.node = node
        self.forwarding_analyzer = ForwardingAnalyzer(node)
        self.channel_fee_policies = node.get_channel_fee_policies()
        self.time_interval_days = None

    def set_fees(self, cltv=14, min_base_fee_msat=20, max_base_fee_msat=400,
                 min_fee_rate=0.000004, max_fee_rate=0.000100, from_days_ago=7,
                 init=False, reckless=False):
        """
        Sets channel fee policies considering different metrics like
        unbalancedness, flow, and demand.

        :param cltv: lock time, don't take a too small number < 5
        :type cltv: int
        :param min_base_fee_msat: minimal base fee in msat
        :type min_base_fee_msat: int
        :param max_base_fee_msat: maximal base fee in msat applied initially
        :type max_base_fee_msat: int
        :param min_fee_rate: the fee rate will be not set lower than
            this amount
        :type min_fee_rate: float
        :param max_fee_rate: maximal fee rate applied initially
        :type max_fee_rate: float
        :param from_days_ago: forwarding history is taken over the past
            from_days_ago days
        :type from_days_ago: int
        :param init: true if fees are set initially with this method
        :type init: bool
        :param reckless: if set, there won't be any user interaction
        :type reckless: bool
        """
        time_end = time.time()
        time_start = time_end - from_days_ago * 24 * 60 * 60
        self.time_interval_days = from_days_ago
        self.forwarding_analyzer.initialize_forwarding_data(
            time_start, time_end)
        self.min_base_fee_msat = min_base_fee_msat
        self.max_base_fee_msat = max_base_fee_msat
        self.min_fee_rate = min_fee_rate
        self.max_fee_rate = max_fee_rate
        self.init = init
        self.cltv = cltv

        channels = self.node.get_all_channels()
        channels_forwarding_stats = \
            self.forwarding_analyzer.get_forwarding_statistics_channels()
        channel_fee_policies = self.new_fee_policy(
            channels, channels_forwarding_stats)

        if not reckless:
            logger.info("Do you want to set these fees? Enter [yes/no]:")
            if yes_no_question():
                self.node.set_channel_fee_policies(channel_fee_policies)
                logger.info("Have set new fee policy.")
            else:
                logger.info("Didn't set new fee policy.")
        else:
            self.node.set_channel_fee_policies(channel_fee_policies)
            logger.info("Have set new fee policy.")

    def new_fee_policy(self, channels, channels_forwarding_stats):
        """
        Calculates and reports the changes to the new fee policy.

        :param channels: basic channel information
        :type channels: dict
        :param channels_forwarding_stats: forwarding information
        :type channels_forwarding_stats: dict

        :return: channel fee policies
        :rtype: dict
        """
        logger.info("Determining new channel policies based on demand.")
        logger.info("Every channel will have a base fee of %d msat and cltv "
                    "of %d.", self.min_base_fee_msat, self.cltv)
        channel_fee_policies = {}

        for channel_id, channel_data in channels.items():
            channel_stats = channels_forwarding_stats.get(channel_id, None)
            if channel_stats is None:
                flow = 0
                fees_sat = 0
                total_forwarding_in = 0
                total_forwarding_out = 0
                total_forwarding = 0
                number_forwardings = 0
                number_forwardings_out = 0
            else:
                flow = channel_stats['flow_direction']
                fees_sat = channel_stats['fees_total'] / 1000
                total_forwarding_in = channel_stats['total_forwarding_in']
                total_forwarding_out = channel_stats['total_forwarding_out']
                total_forwarding = total_forwarding_in + total_forwarding_out
                number_forwardings = channel_stats['number_forwardings']
                number_forwardings_out = channel_stats[
                    'number_forwardings_out']

            ub = channel_data['unbalancedness']
            capacity = channel_data['capacity']

            fee_rate = \
                self.channel_fee_policies[
                    channel_data['channel_point']]['fee_rate']
            base_fee_msat = \
                self.channel_fee_policies[
                    channel_data['channel_point']]['base_fee_msat']

            logger.info(">>> New channel policy for channel %s", channel_id)
            logger.info(
                "    ub: %0.2f flow: %0.2f, fees: %1.3f sat, cap: %d sat, "
                "nfwd: %d, in: %d sat, out: %d sat.", ub, flow, fees_sat,
                capacity, number_forwardings, total_forwarding_in,
                total_forwarding_out)

            # FEE RATES
            # we want to give the demand the highest weight of the three
            # indicators
            wgt_demand = 1.3
            wgt_ub = 1.0
            wgt_flow = 0.6

            factor_demand = self.factor_demand_fee_rate(total_forwarding_out)
            factor_unbalancedness = self.factor_unbalancedness(ub)
            factor_flow = self.factor_flow(flow)

            # in the case where no forwarding was done, ignore the flow factor
            if total_forwarding == 0:
                wgt_flow = 0

            # calculate weighted change
            weighted_change = (
                wgt_ub * factor_unbalancedness +
                wgt_flow * factor_flow +
                wgt_demand * factor_demand
            ) / (wgt_ub + wgt_flow + wgt_demand)

            logger.info(
                "    Change factors: demand: %1.3f, "
                "unbalancedness %1.3f, flow: %1.3f. Weighted change: %1.3f",
                factor_demand, factor_unbalancedness, factor_flow,
                weighted_change)

            # if we initialize the fee optimization, we want to start with
            # reasonable starting values
            if self.init:
                if weighted_change < 1:
                    fee_rate_new = self.min_fee_rate
                else:
                    fee_rate_new = self.max_fee_rate / 2
            else:
                # round down to 6 digits, as this is the expected data for
                # the api
                fee_rate_new = round(fee_rate * weighted_change, 6)

                # if the fee rate is too low, cap it, as we don't want to
                # necessarily have too low fees, limit also from top
                fee_rate_new = min(max(self.min_fee_rate, fee_rate_new),
                                   self.max_fee_rate)

                # THRESHOLD MODES
                # LOW UNBALANCEDNESS
                # in the case of low fees and no forwarding traffic go into
                # completely open mode
                if fee_rate <= self.min_fee_rate and ub < THRESHOLD_UB_OPEN \
                        and total_forwarding_out == 0:
                    logger.info("    > Open mode.")
                    fee_rate_new = 0
                # if we were in full open mode, leave it when forwardings
                # rebalanced the channel
                elif fee_rate == 0 and total_forwarding_out > 0 and \
                        ub > THRESHOLD_UB_OPEN:
                    logger.info("    > Leaving open mode.")
                    fee_rate_new = self.min_fee_rate

                # HIGH UNBALANCEDNESS
                if ub > THRESHOLD_UB_CLOSE and total_forwarding_out == 0:
                    logger.info("    > Fee locked mode.")
                    fee_rate_new = FEE_LOCK_RATE
                elif fee_rate == FEE_LOCK_RATE and ub < THRESHOLD_UB_CLOSE:
                    logger.info("    > Leaving fee locked mode.")
                    fee_rate_new = self.max_fee_rate / 2

            logger.info("    Fee rate: %1.6f -> %1.6f",
                        fee_rate, fee_rate_new)

            # BASE FEES
            factor_base_fee = self.factor_demand_base_fee(
                number_forwardings_out)
            base_fee_msat_new = base_fee_msat * factor_base_fee
            if self.init:
                if factor_base_fee < 1:
                    base_fee_msat_new = self.min_base_fee_msat
                else:
                    base_fee_msat_new = self.max_base_fee_msat
            else:
                base_fee_msat_new = int(max(self.min_base_fee_msat, base_fee_msat_new))

            logger.info("    Base fee: %4d -> %4d (factor %1.3f)",
                        base_fee_msat, base_fee_msat_new, factor_base_fee)


            # give parsable output
            logger.debug(
                f"stats: {time.time():.0f} {channel_id} "
                f"{total_forwarding_in} {total_forwarding_out} "
                f"{ub:.3f} {flow:.3f} "
                f"{fees_sat:.3f} {capacity} {factor_demand:.3f} "
                f"{factor_unbalancedness:.3f} {factor_flow:.3f} "
                f"{weighted_change:.3f} {fee_rate:.6f} {fee_rate_new:.6f} "
                f"{number_forwardings} {number_forwardings_out} "
                f"{factor_base_fee:.3f} {base_fee_msat} {base_fee_msat_new}")

            channel_fee_policies[channel_data['channel_point']] = {
                'base_fee_msat': base_fee_msat_new,
                'fee_rate': fee_rate_new,
                'cltv': self.cltv,
            }

        return channel_fee_policies

    @staticmethod
    def factor_unbalancedness(ub):
        """
        Calculates a change rate for the unbalancedness.

        The lower the unbalancedness, the lower the fee rate should be.
        This encourages outward flow through this channel.
        :param ub: in [-1 ... 1]
        :type ub: float
        :return: [1-c_max, 1+c_max]
        :rtype: float
        """
        # maximal change
        c_max = 0.5
        # give unbalancedness a more refined weight
        rescale = 0.5

        c = 1 + ub * rescale
        # limit the change
        if c > 1:
            return min(c, 1 + c_max)
        else:
            return max(c, 1 - c_max)

    @staticmethod
    def factor_flow(flow):
        """
        Calculates a change rate for the flow rate.

        If forwardings are predominantly flowing outward, we want to increase
        the fee rate, because there seems to be demand and the trend is bad.
        :param flow: [-1 ... 1]
        :type flow: float
        :return: [1-c_max, 1+c_max]
        :rtype: float
        """
        c_max = 0.5
        rescale = 0.5
        c = 1 + flow * rescale

        # limit the change
        if c > 1:
            return min(c, 1 + c_max)
        else:
            return max(c, 1 - c_max)

    def factor_demand_fee_rate(self, amount_out):
        """
        Calculates a change factor for a channel by taking into account
        the amount transacted in a time interval compared to a fixed amount.

        The higher the amount forwarded, the larger the fee rate should be. The
        amount forwarded is estimated dividing the fees_sat with the current
        fee_rate.

        :param amount_out: amount transacted outwards for the channel
        :type amount_out: float

        :return: [1-c_max, 1+c_max]
        :rtype: float
        """
        logger.info("    Outward forwarded amount: %6.0f",
                    amount_out)
        rate = amount_out / self.time_interval_days

        c_min = 0.25  # change by 25% downwards
        c_max = 1.00  # change by 100% upwards

        # rate_target = 0.10 * capacity / 7  # target rate is 10% of capacity
        rate_target = 500000 / 7  # target rate is fixed 500000 sat per week

        c = c_min * (rate / rate_target - 1) + 1

        return min(c, 1 + c_max)

    def factor_demand_base_fee(self, num_fwd_out):
        """
        Calculates a change factor by taking into account the number of
        transactions transacted in a time interval compared to a fixed number
        of transactions.

        :param num_fwd_out: number of outward forwardings
        :type num_fwd_out: int
        :return: [1-c_max, 1+c_max]
        :rtype: float
        """
        logger.info(
            "    Number of outward forwardings: %6.0f", num_fwd_out)
        c_min = 0.25  # change by 25% downwards
        c_max = 1.00  # change by 100% upwards

        num_fwd_target = 5 / 7
        c = c_min * ((num_fwd_out / self.time_interval_days)
                     / num_fwd_target - 1) + 1

        return min(c, 1 + c_max)


if __name__ == '__main__':
    from lndmanage.lib.node import LndNode
    import logging.config
    from lndmanage import settings

    logging.config.dictConfig(settings.logger_config)

    nd = LndNode()
    fee_setter = FeeSetter(nd)
    fee_setter.set_fees()
